# -*- coding: utf-8 -*-
import os
import re
from typing import List, Tuple

from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
from odoo import http
from odoo.http import request
import logging
_logger = logging.getLogger(__name__)

# Optional libraries
try:
    from pypdf import PdfReader
    _PDF = True
except Exception:  # pragma: no cover
    PdfReader = None
    _PDF = False

try:
    # Newer OpenAI lib (client style) or fallback
    import openai  # noqa: F401
    _OPENAI = True
except Exception:  # pragma: no cover
    openai = None  # type: ignore
    _OPENAI = False

try:
    import google.generativeai as genai  # noqa: F401
    _GEMINI = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore
    _GEMINI = False


def _normalize_words(text: str) -> List[str]:
    try:
        return re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    except Exception:
        return []


def _score_overlap(context_piece: str, query: str) -> int:
    q = set(_normalize_words(query))
    c = set(_normalize_words(context_piece))
    return len(q.intersection(c))


def _walk_pdfs(folder: str, max_files: int = 40, max_pages: int = 40) -> List[Tuple[str, str]]:
    docs = []
    if not _PDF:
        _logger.info("pypdf not installed; PDF context disabled.")
        return docs
    try:
        files = []
        for root, _dirs, filenames in os.walk(folder):
            for fn in filenames:
                if fn.lower().endswith(".pdf"):
                    files.append(os.path.join(root, fn))
            if len(files) >= max_files:
                break
        files = files[:max_files]
        for path in files:
            try:
                reader = PdfReader(path)
                pages = min(len(reader.pages), max_pages)
                for i in range(pages):
                    try:
                        txt = reader.pages[i].extract_text() or ""
                    except Exception:  # tolerate page extraction issues
                        txt = ""
                    if txt.strip():
                        docs.append((path, txt.strip()))
            except Exception:
                _logger.info("Skipping unreadable PDF: %s", path)
        return docs
    except Exception as e:
        _logger.exception("Error walking PDFs in %s: %s", folder, e)
        return docs


def _best_chunks(docs: List[Tuple[str, str]], query: str, k: int = 3) -> List[str]:
    scored = []
    for _path, text in docs:
        scored.append((_score_overlap(text, query), text))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [t[1] for t in scored[:k] if t[0] > 0]


def _question_allowed(question: str, rules_block: str) -> bool:
    """Allow if rules empty; else require a match against any regex line."""
    rules = (rules_block or "").splitlines()
    active = []
    for line in rules:
        s = (line or "").strip()
        if not s or s.startswith("#"):
            continue
        try:
            active.append(re.compile(s, re.IGNORECASE))
        except re.error:
            _logger.info("Invalid regex skipped in allowed_questions: %s", s)
            continue
    if not active:
        return True
    return any(p.search(question or "") for p in active)


class AIAssistantController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", methods=["POST"], csrf=False)
    def can_load(self):
        try:
            user = request.env.user
            show = bool(user and not user._is_public() and user.has_group("website_ai_chat_min.group_ai_chat_user"))
            return {"show": show}
        except Exception as e:
            _logger.exception("can_load failed: %s", e)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", methods=["POST"], csrf=True)
    def send(self, message=None):
        try:
            user = request.env.user
            if not user.has_group("website_ai_chat_min.group_ai_chat_user"):
                # Return JSON error instead of raising, to avoid generic network error in UI
                return {"ok": False, "error": _("You do not have access to AI Chat.")}

            if not isinstance(message, str) or not message.strip():
                return {"ok": False, "error": _("Please enter a question.")}
            if len(message) > 2000:
                return {"ok": False, "error": _("Message is too long (limit 2000 characters).")}

            ICP = request.env["ir.config_parameter"].sudo()
            provider = (ICP.get_param("website_ai_chat_min.provider", default="openai") or "openai").strip().lower()
            api_key = ICP.get_param("website_ai_chat_min.api_key", default="") or ""
            model_name = (ICP.get_param("website_ai_chat_min.model", default="gpt-4o-mini") or "gpt-4o-mini").strip()
            folder = (ICP.get_param("website_ai_chat_min.docs_folder", default="") or "").strip()
            sys_instruction = ICP.get_param("website_ai_chat_min.sys_instruction", default="") or ""
            allowed_questions = ICP.get_param("website_ai_chat_min.allowed_questions", default="") or ""
            context_only_str = ICP.get_param("website_ai_chat_min.context_only", default="True") or "True"
            try:
                context_only = tools.str2bool(context_only_str)
            except Exception:
                context_only = True

            if not api_key:
                return {"ok": False, "error": _("API key not configured. Ask an admin.")}

            # Guardrails (use default if not provided)
            guard = sys_instruction.strip() or _("Answer only with facts from the provided context. If unsure, say 'I don't know'.")

            # Positive allow-list
            if not _question_allowed(message, allowed_questions):
                return {"ok": False, "error": _("Your question is not within the allowed scope.")}

            # Build context from PDFs (if folder configured and valid absolute path)
            context_chunks: List[str] = []
            if folder:
                real = os.path.realpath(folder)
                if not os.path.isabs(real) or not os.path.isdir(real):
                    return {"ok": False, "error": _("Invalid documents folder path.")}
                docs = _walk_pdfs(real, max_files=40, max_pages=40)
                if docs:
                    context_chunks = _best_chunks(docs, message, k=3)

            context_text = "\n\n".join(context_chunks).strip()
            if context_only and not context_text:
                return {"ok": False, "error": _("I don't know based on the current documents.")}

            # Provider-specific call
            reply = ""
            if provider == "gemini":
                if not _GEMINI:
                    return {"ok": False, "error": _("Google Gemini client not installed on server.")}
                try:
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel(model_name)
                    prompt = f"{guard}\n\n[CONTEXT]\n{context_text or '(none)'}\n\n[QUESTION]\n{message.strip()}"
                    res = model.generate_content(prompt)
                    reply = (getattr(res, "text", "") or "").strip()
                except Exception as e:
                    _logger.exception("Gemini error: %s", e)
                    return {"ok": False, "error": _("AI provider error (Gemini).")}
            else:
                if not _OPENAI:
                    return {"ok": False, "error": _("OpenAI client not installed on server.")}
                # OpenAI: try new client style, fallback to legacy
                messages = [
                    {"role": "system", "content": guard},
                    {"role": "user", "content": f"Context:\n{context_text or '(none)'}\n\nQuestion: {message.strip()}"},
                ]
                try:
                    # Try v1 client style if available
                    from openai import OpenAI  # type: ignore
                    client = OpenAI(api_key=api_key)
                    comp = client.chat.completions.create(model=model_name, messages=messages)
                    reply = (comp.choices[0].message.content or "").strip()
                except Exception:
                    try:
                        # Legacy style
                        openai.api_key = api_key  # type: ignore[attr-defined]
                        comp = openai.ChatCompletion.create(model=model_name, messages=messages)  # type: ignore[attr-defined]
                        reply = (comp["choices"][0]["message"]["content"] or "").strip()
                    except Exception as e2:
                        _logger.exception("OpenAI error: %s", e2)
                        return {"ok": False, "error": _("AI provider error (OpenAI).")}

            return {"ok": True, "reply": reply or _("(no reply)")}
        except (UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, MissingError) as e:
            _logger.exception("Chat error: %s", e)
            return {"ok": False, "error": tools.ustr(e)}
        except Exception as e:
            _logger.exception("Unexpected chat error: %s", e)
            return {"ok": False, "error": _("Unexpected error. Please contact your administrator.")}
        finally:
            # Placeholder for any cleanup if needed in the future
            pass
