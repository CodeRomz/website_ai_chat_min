# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import os
import re
import time
from typing import List, Tuple

# PDF parsing
try:
    from pypdf import PdfReader
    _PDF = True
except Exception:
    _PDF = False

# Providers (optional)
try:
    import google.generativeai as genai
    _GEMINI = True
except Exception:
    _GEMINI = False

try:
    import openai
    _OPENAI = True
except Exception:
    _OPENAI = False


# --------- RAG-lite helpers (keyword overlap, chunking) ---------
_STOPWORDS = set("""
a an the and or of to in on for with by from as at is are was were be been being that this those these there here it its it's
""".split())


def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    tokens = re.split(r"[^a-z0-9]+", text)
    return [t for t in tokens if t and t not in _STOPWORDS]


def _walk_pdfs(folder: str, max_files: int = 40, max_pages: int = 40) -> List[Tuple[str, str]]:
    """Return list of (path, text) for PDFs under folder with conservative limits."""
    out = []
    if not _PDF:
        return out
    count = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            if count >= max_files:
                return out
            path = os.path.join(root, name)
            try:
                reader = PdfReader(path)
                text = []
                pages = min(len(reader.pages), max_pages)
                for i in range(pages):
                    try:
                        text.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        continue
                content = "\n".join(text).strip()
                if content:
                    out.append((path, content))
                    count += 1
            except Exception as e:
                _logger.info("Skipped unreadable PDF %s: %s", path, e)
    return out


def _best_chunks(docs: List[Tuple[str, str]], query: str, chunk_size: int = 1200, top_k: int = 3) -> List[str]:
    """Split text into fixed-size chunks; score by token overlap; return top_k chunks."""
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return []

    scored = []
    for _path, text in docs:
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            c_tokens = set(_tokenize(chunk))
            if not c_tokens:
                continue
            # Jaccard-like simple score
            score = len(q_tokens & c_tokens) / max(1, len(q_tokens))
            if score > 0:
                scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _s, c in scored[:top_k]]


def _question_allowed(question: str, rules_block: str) -> bool:
    """Optional allowlist: one regex per line; if empty -> allowed."""
    if not rules_block:
        return True
    for line in (rules_block or "").splitlines():
        pat = line.strip()
        if not pat:
            continue
        try:
            if re.search(pat, question, flags=re.IGNORECASE):
                return True
        except re.error:
            # ignore invalid regex
            continue
    return False


class AIAssistantController(http.Controller):

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def ai_chat_send(self, message=None):
        """
        Minimal chat endpoint with folder-grounding and guardrails.
        Only for authenticated users in the AI Chat User group.
        """
        # Group check
        if not request.env.user.has_group("website_ai_chat_min.group_ai_chat_user"):
            raise AccessDenied(_("You do not have access to AI Chat."))

        if not message or not isinstance(message, str):
            return {"ok": False, "error": _("Empty message.")}
        if len(message) > 2000:
            return {"ok": False, "error": _("Message too long (max 2000 chars).")}

        user = request.env.user
        is_admin_cfg = user.has_group("website_ai_chat_min.group_ai_chat_admin")

        ICP = request.env["ir.config_parameter"].sudo()
        provider = ICP.get_param("website_ai_chat_min.provider", "gemini")
        api_key = ICP.get_param("website_ai_chat_min.api_key") or ""
        model = ICP.get_param("website_ai_chat_min.model", "gemini-2.0-flash-lite")

        docs_folder_cfg = (ICP.get_param("website_ai_chat_min.docs_folder") or "").strip()
        sys_instruction = (ICP.get_param("website_ai_chat_min.system_instruction") or "").strip()
        allowed_rules = ICP.get_param("website_ai_chat_min.allowed_questions") or ""
        context_only = tools.str2bool(ICP.get_param("website_ai_chat_min.context_only") or "True")

        if not api_key:
            return {"ok": False, "error": _("API key not configured. Ask an admin.")}

        # Normalize & re-check path to avoid surprises (symlinks, ../)
        docs_folder = os.path.realpath(docs_folder_cfg)
        if not docs_folder or not os.path.isabs(docs_folder) or not os.path.isdir(docs_folder):
            return {"ok": False, "error": _("Invalid PDFs folder path.")}

        # Allowed question check
        if not _question_allowed(message, allowed_rules):
            return {"ok": False, "error": _("Your question is not within the allowed scope.")}

        if not _PDF:
            return {"ok": False, "error": _("PDF parser (pypdf) not installed on server.")}

        t0 = time.time()
        try:
            # Load & retrieve (RAG-lite)
            docs = _walk_pdfs(docs_folder, max_files=40, max_pages=40)
            chunks = _best_chunks(docs, message, chunk_size=1200, top_k=3)

            if context_only and not chunks:
                return {
                    "ok": True,
                    "reply": _("I don’t know based on the current documents."),
                    "latency_ms": int((time.time() - t0) * 1000),
                }

            guard = sys_instruction or "Answer only with facts from the provided context. If unsure, say 'I don't know'."
            context_block = "\n\n---\n".join(chunks) if chunks else "(no relevant context)"
            user_prompt = (
                f"{guard}\n\n"
                f"Context (excerpts from company PDFs):\n{context_block}\n\n"
                f"Question: {message}\n"
                f"If the answer is not supported by the context, say: I don't know."
            )

            # Provider calls
            reply = ""
            if provider == "gemini":
                if not _GEMINI:
                    return {"ok": False, "error": _("Gemini client not installed on server.")}
                genai.configure(api_key=api_key)
                model_client = genai.GenerativeModel(model)
                prompt = f"[RULES]\n{guard}\n\n[CONTEXT]\n{context_block}\n\n[QUESTION]\n{message}\n\n[RESPONSE]"
                resp = model_client.generate_content(prompt)
                reply = getattr(resp, "text", "") or ""

            elif provider == "openai":
                if not _OPENAI:
                    return {"ok": False, "error": _("OpenAI client not installed on server.")}
                try:
                    client = openai.OpenAI(api_key=api_key)
                    r = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": guard},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.0,
                    )
                    reply = r.choices[0].message.content.strip()
                except Exception:
                    # Legacy fallback (older openai package)
                    openai.api_key = api_key
                    r = openai.ChatCompletion.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": guard},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.0,
                    )
                    reply = r["choices"][0]["message"]["content"].strip()
            else:
                return {"ok": False, "error": _("Unsupported provider: %s") % provider}

            if not reply:
                reply = _("(No response)")
            return {"ok": True, "reply": reply, "latency_ms": int((time.time() - t0) * 1000)}

        except (UserError, AccessError, ValidationError) as e:
            return {"ok": False, "error": tools.ustr(e)}
        except Exception as e:
            _logger.exception("AI RAG-lite error: %s", e)
            return {"ok": False, "error": _("AI provider or PDF processing error.")}
