from odoo import models, fields, api, tools, _
from odoo.exceptions import (
    UserError, ValidationError, RedirectWarning, AccessDenied,
    AccessError, CacheMiss, MissingError
)
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import json
import os
import time
import re as re_std

try:
    import regex as regex_safe  # type: ignore
except Exception:
    regex_safe = None

DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _icp():
    return request.env["ir.config_parameter"].sudo()


def _get_icp_param(name, default=""):
    try:
        val = _icp().get_param(name, default)
        return val if val not in (None, "") else default
    except Exception as e:
        _logger.warning("ICP get_param failed for %s: %s", name, tools.ustr(e))
        return default


def _is_logged_in(env):
    try:
        return bool(env.user and not env.user._is_public())
    except Exception:
        return False


def _require_group_if_configured(env) -> bool:
    xmlid = _get_icp_param("website_ai_chat_min.require_group_xmlid", "")
    if not xmlid:
        return True
    try:
        return env.user.has_group(xmlid)
    except Exception:
        return False


def _can_show_widget(env) -> bool:
    return _is_logged_in(env) and _require_group_if_configured(env)


def _get_rate_limits():
    try:
        max_req = int(_get_icp_param("website_ai_chat_min.rate_limit_max", DEFAULT_RATE_LIMIT_MAX))
    except Exception:
        max_req = DEFAULT_RATE_LIMIT_MAX
    try:
        window = int(_get_icp_param("website_ai_chat_min.rate_limit_window", DEFAULT_RATE_LIMIT_WINDOW))
    except Exception:
        window = DEFAULT_RATE_LIMIT_WINDOW
    return max(1, max_req), max(1, window)


def _client_ip():
    try:
        xfwd = request.httprequest.headers.get("X-Forwarded-For", "")
        ip = (xfwd.split(",")[0].strip() if xfwd else request.httprequest.remote_addr) or "0.0.0.0"
        return ip
    except Exception:
        return "0.0.0.0"


def _throttle() -> bool:
    try:
        max_req, window = _get_rate_limits()
        now = time.time()
        user_id = request.env.uid or 0
        ip = _client_ip()
        key = f"website_ai_chat_min_rl:{user_id}:{ip}"
        hist = request.session.get(key, [])
        hist = [t for t in hist if now - t < window]
        allowed = len(hist) < max_req
        if allowed:
            hist.append(now)
            if len(hist) > max_req:
                hist = hist[-max_req:]
            request.session[key] = hist
            request.session.modified = True
        return allowed
    except Exception as e:
        _logger.warning("Throttle error: %s", tools.ustr(e))
        return True


def _normalize_message_from_request(question_param=None) -> str:
    msg = (question_param or "").strip()
    if msg:
        return msg
    try:
        raw = request.httprequest.get_data(cache=False, as_text=True)
        if raw:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                params = payload.get("params")
                if isinstance(params, dict):
                    msg = (params.get("message") or params.get("question") or "").strip()
                    if msg:
                        return msg
                msg = (payload.get("message") or payload.get("question") or "").strip()
                if msg:
                    return msg
    except Exception:
        pass
    return ""


def _match_allowed(pattern: str, text: str, timeout_ms=60) -> bool:
    if not pattern:
        return True
    try:
        if regex_safe:
            return bool(
                regex_safe.search(pattern, text, flags=regex_safe.I | regex_safe.M, timeout=timeout_ms)
            )
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception as e:
        _logger.warning("Invalid allowed_regex or match error: %s", tools.ustr(e))
        return False


def _read_pdf_snippets(root_folder: str, query: str) -> dict:
    try:
        import pypdf
    except Exception as e:
        _logger.warning("pypdf not installed: %s", tools.ustr(e))
        return {}

    result = {}
    ql = query.lower()
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        text = (page.extract_text() or "").strip()
                        if ql in text.lower():
                            result[fn] = text[:1000].replace("\n", " ")
                            break
            except Exception as e:
                _logger.warning("Error reading PDF %s: %s", path, tools.ustr(e))
    return result


def _get_ai_config():
    provider = _get_icp_param("website_ai_chat_min.ai_provider", "openai")
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    model = _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "")
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
    docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "")
    only_docs = tools.str2bool(_get_icp_param("website_ai_chat_min.answer_only_from_docs", "0"))
    return provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs


def _call_openai(api_key, model, system_prompt, user_text) -> str:
    from openai import OpenAI  # type: ignore
    client = OpenAI(api_key=api_key, timeout=15)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})
    resp = client.chat.completions.create(
        model=(model or OPENAI_DEFAULT_MODEL),
        messages=messages,
        max_tokens=512,
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def _call_gemini(api_key, model, system_prompt, user_text) -> str:
    import google.generativeai as genai  # type: ignore
    genai.configure(api_key=api_key)
    model_name = model or GEMINI_DEFAULT_MODEL
    prompt = (system_prompt + "\n\n" if system_prompt else "") + (user_text or "")
    r = genai.GenerativeModel(model_name).generate_content(
        [prompt],
        request_options={"timeout": 15},
        generation_config={"temperature": 0.2, "max_output_tokens": 512},
    )
    return (getattr(r, "text", None) or "").strip()


class WebsiteAIChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=False, methods=["POST"])
    def can_load(self):
        try:
            show = _can_show_widget(request.env)
            _logger.info("[website_ai_chat_min] can_load show=%s", show)
            return {"show": bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        if not _require_group_if_configured(request.env):
            raise AccessDenied("You do not have access to AI Chat.")

        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        try:
            _logger.info("[website_ai_chat_min] /ai_chat/send uid=%s len=%s ip=%s", request.env.uid, len(q), _client_ip())
        except Exception:
            pass

        try:
            provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs = _get_ai_config()
            if not api_key:
                return {"ok": False, "reply": _("AI provider API key is not configured. Please contact the administrator.")}

            if allowed_regex and not _match_allowed(allowed_regex, q):
                return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

            doc_snippets = {}
            if docs_folder and os.path.isdir(docs_folder):
                doc_snippets = _read_pdf_snippets(docs_folder, q)

            if only_docs and not doc_snippets:
                return {"ok": True, "reply": _("I don’t know based on the current documents.")}

            context = "\n".join([f"From {fname}: {text}" for fname, text in doc_snippets.items()])
            full_prompt = (system_prompt + "\n\n" if system_prompt else "") + (context + "\n\n" if context else "") + q

            if provider == "gemini":
                reply = _call_gemini(api_key, model, system_prompt, full_prompt)
            else:
                reply = _call_openai(api_key, model, system_prompt, full_prompt)

            return {"ok": True, "reply": reply or _("(No answer returned.)")}

        except Exception as e:
            _logger.error("AI Chat unexpected error: %s", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("An unexpected error occurred. Please try again later.")}