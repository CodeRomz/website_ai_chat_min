# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import os
import re

# Optional dependencies
try:
    from pypdf import PdfReader
    _PDF_OK = True
except Exception as e:
    _PDF_OK = False
    _logger.info("pypdf not installed or failed to import: %s", e)

# OpenAI SDK (new and fallback old-style)
_OPENAI_NEW = None
_OPENAI_OLD = None
try:
    from openai import OpenAI as _OpenAIClient  # new SDK
    _OPENAI_NEW = _OpenAIClient
except Exception:
    try:
        import openai as _openai  # classic SDK
        _OPENAI_OLD = _openai
    except Exception as e:
        _logger.info("openai not installed: %s", e)

# Google Generative AI
try:
    import google.generativeai as genai
    _GEMINI_OK = True
except Exception as e:
    _GEMINI_OK = False
    _logger.info("google-generativeai not installed: %s", e)


def _read_pdf_snippets(root_folder: str, query: str, max_files: int = 40, max_pages_per_file: int = 40) -> str:
    """
    Naive keyword-based PDF context gathering.
    Returns concatenated snippets from up to max_files PDFs and up to max_pages_per_file per file.
    """
    if not _PDF_OK or not root_folder or not os.path.isdir(root_folder):
        return ""

    snippets = []
    q_low = (query or "").lower().split()
    num_files = 0

    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(dirpath, fn)
            try:
                reader = PdfReader(pdf_path)
            except Exception as e:
                _logger.warning("Failed to read PDF %s: %s", pdf_path, e)
                continue

            pages = reader.pages[:max_pages_per_file]
            file_hits = []
            for p in pages:
                try:
                    txt = (p.extract_text() or "").strip()
                except Exception as e:
                    _logger.debug("PDF text extraction error on %s: %s", pdf_path, e)
                    txt = ""
                if not txt:
                    continue
                low = txt.lower()
                score = sum(1 for w in q_low if w and w in low)
                if score:
                    # take a small slice
                    slice_txt = txt[:1200]
                    file_hits.append(f"[{os.path.basename(pdf_path)}]\n{slice_txt}")
                if len(file_hits) >= 2:
                    break  # cap per file

            if file_hits:
                snippets.extend(file_hits)
                num_files += 1
                if num_files >= max_files:
                    break
        if num_files >= max_files:
            break

    return "\n\n".join(snippets[:12])  # cap number of snippets


def _call_openai(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    """Call OpenAI using either new or old SDK, transparently."""
    if _OPENAI_NEW:
        client = _OPENAI_NEW(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()
    elif _OPENAI_OLD:
        _OPENAI_OLD.api_key = api_key
        resp = _OPENAI_OLD.ChatCompletion.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return (resp["choices"][0]["message"]["content"] or "").strip()
    raise UserError(_("OpenAI SDK not available. Please install 'openai'. "))


def _call_gemini(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    if not _GEMINI_OK:
        raise UserError(_("Google Generative AI SDK not available. Please install 'google-generativeai'."))
    genai.configure(api_key=api_key)
    # Gemini system prompt can be prefixed to user content for simplicity
    model_obj = genai.GenerativeModel(model)
    full_prompt = f"{system_prompt}\n\nUser:\n{user_prompt}"
    resp = model_obj.generate_content(full_prompt)
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


class WebsiteAIChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=False)
    def can_load(self):
        """Decide whether to render the chat bubble."""
        user = request.env.user
        show = False
        try:
            if user and not user._is_public():
                show = bool(
                    user.has_group("website_ai_chat_min.group_ai_chat_user")
                    or user.has_group("base.group_system")
                )
        except Exception as e:
            _logger.debug("can_load group check failed: %s", e)
            show = False
        return {"show": show}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True)
    def send(self, question=None):
        """Handle a chat message, optionally grounding with PDF context."""
        icp = request.env["ir.config_parameter"].sudo()
        provider = icp.get_param("website_ai_chat_min.ai_provider", "openai")
        api_key = icp.get_param("website_ai_chat_min.ai_api_key", "")
        model = icp.get_param("website_ai_chat_min.ai_model", "gpt-3.5-turbo")
        docs_folder = icp.get_param("website_ai_chat_min.docs_folder", "")
        sys_instruction = icp.get_param("website_ai_chat_min.sys_instruction", "")
        allowed_patterns = icp.get_param("website_ai_chat_min.allowed_questions", "")
        context_only = tools.str2bool(icp.get_param("website_ai_chat_min.context_only", "False"))

        if not api_key:
            return {"ok": False, "reply": _("API key not configured. Please ask an administrator.")}

        q = (question or "").strip()
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 characters).")}

        if allowed_patterns:
            try:
                lines = [l for l in (allowed_patterns or "").splitlines() if l.strip()]
                if lines:
                    import re as _re
                    if not any(_re.search(pat, q, flags=_re.I) for pat in lines):
                        return {"ok": False, "reply": _("Your question is not within the allowed scope.")}
            except Exception as e:
                _logger.warning("Invalid allowed_questions regex: %s", e)

        context_snippets = _read_pdf_snippets(docs_folder, q) if docs_folder else ""
        if context_only and not context_snippets:
            return {"ok": True, "reply": _("I don’t know based on the current documents.")}

        system_prompt = (sys_instruction or "").strip()
        if context_snippets:
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") +                 _("Use ONLY the following document excerpts when answering:\n{ctx}").format(ctx=context_snippets)

        try:
            if provider == "gemini":
                reply = _call_gemini(api_key, model, system_prompt or "You are a helpful assistant.", q)
            else:
                reply = _call_openai(api_key, model, system_prompt or "You are a helpful assistant.", q)
        except Exception as e:
            _logger.exception("AI provider error: %s", e)
            return {"ok": False, "reply": _("AI provider error: %s") % (tools.ustr(e),)}

        reply = reply or _("(No answer returned.)")
        return {"ok": True, "reply": reply}
