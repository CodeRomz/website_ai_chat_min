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

# Try faster/safer regex with timeout; fallback to stdlib
try:
    import regex as regex_safe  # type: ignore
except Exception:  # pragma: no cover
    regex_safe = None


# -----------------------------------------------------------------------------
# Tunables (override with ICP where applicable)
# -----------------------------------------------------------------------------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15  # seconds
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
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
    """Any authenticated user (portal or internal)."""
    try:
        return bool(env.user and not env.user._is_public())
    except Exception:
        return False


def _require_group_if_configured(env) -> bool:
    """
    Optional extra gate:
    - ir.config_parameter: website_ai_chat_min.require_group_xmlid
      e.g. 'website_ai_chat_min.group_ai_chat_user'
    If empty, allow all logged-in users. If set, require membership.
    """
    xmlid = _get_icp_param("website_ai_chat_min.require_group_xmlid", "")
    if not xmlid:
        return True
    try:
        return env.user.has_group(xmlid)
    except Exception:
        # If group not found or error -> safer to deny
        return False


def _can_show_widget(env) -> bool:
    """Login-only + optional group."""
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
    """Best-effort client IP (trusts first X-Forwarded-For hop)."""
    try:
        xfwd = request.httprequest.headers.get("X-Forwarded-For", "")
        ip = (xfwd.split(",")[0].strip() if xfwd else request.httprequest.remote_addr) or "0.0.0.0"
        return ip
    except Exception:
        return "0.0.0.0"


def _throttle() -> bool:
    """
    Per-session + per-user/IP throttle in Werkzeug session.
    For multi-node deployments, replace with Redis-based limiter.
    """
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
        return True  # fail-open to avoid blocking users


def _get_incoming_payload() -> dict:
    """Parse JSON body for both JSON-RPC and raw JSON."""
    try:
        raw = request.httprequest.get_data(cache=False, as_text=True)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _normalize_message_from_request(question_param=None) -> str:
    """
    Accepts:
    - JSON-RPC: {"params":{"message":"..."}} / {"params":{"question":"..."}}
    - Raw JSON: {"message":"..."} or {"question":"..."}
    - Direct kwarg: question=...
    """
    msg = (question_param or "").strip()
    if msg:
        return msg
    payload = _get_incoming_payload()
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            msg = (params.get("message") or params.get("question") or "").strip()
            if msg:
                return msg
        msg = (payload.get("message") or payload.get("question") or "").strip()
        if msg:
            return msg
    return ""


def _match_allowed(pattern: str, text: str, timeout_ms=60) -> bool:
    """Time-boxed regex allow-list check."""
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


def _read_pdf_snippets(
    root_folder: str,
    query: str,
    max_files=40,
    max_pages=40,
    per_page_chars=1200,
    max_hits=12,
    max_runtime_ms=350,
):
    """Tiny keyword scan with hard ceilings to avoid abuse."""
    try:
        import pypdf  # type: ignore
    except Exception as e:
        _logger.info("pypdf not installed: %s", tools.ustr(e))
        return []

    start = time.time()
    hits = []
    ql = (query or "").lower()

    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            if len(hits) >= max_hits or max_files <= 0:
                return hits
            if (time.time() - start) * 1000 > max_runtime_ms:
                _logger.info("PDF scan aborted due to time ceiling (%sms)", max_runtime_ms)
                return hits
            max_files -= 1
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for i, page in enumerate(reader.pages[:max_pages]):
                        text = (page.extract_text() or "")[:per_page_chars]
                        if ql and ql in text.lower():
                            hits.append(f"[{fn} p.{i+1}] {text.strip()}")
                            if len(hits) >= max_hits:
                                return hits
                            if (time.time() - start) * 1000 > max_runtime_ms:
                                _logger.info("PDF scan aborted mid-file due to time ceiling")
                                return hits
            except Exception as e:
                _logger.warning("Failed reading PDF %s: %s", path, tools.ustr(e))
    return hits


def _get_ai_config():
    provider = os.getenv("AI_PROVIDER") or _get_icp_param("website_ai_chat_min.ai_provider", "openai")
    api_key = os.getenv("AI_API_KEY") or _get_icp_param("website_ai_chat_min.ai_api_key", "")
    model = os.getenv("AI_MODEL") or _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "") or ""
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "") or ""
    docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "") or ""
    only_docs = tools.str2bool(_get_icp_param("website_ai_chat_min.answer_only_from_docs", "0"))
    return provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs


def _call_openai(api_key, model, system_prompt, user_text) -> str:
    # OpenAI: short timeout and token caps
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


# -----------------------------------------------------------------------------
# Controllers
# -----------------------------------------------------------------------------
class WebsiteAIChatController(http.Controller):
    """Login-only bubble; page and send endpoint require auth='user'."""

    @http.route("/ai-chat", type="http", auth="user", website=True, methods=["GET"])
    def ai_chat_page(self, **kw):
        vals = {
            "privacy_url": _icp().get_param("website_ai_chat_min.privacy_url", default=""),
        }
        return request.render("website_ai_chat_min.ai_chat_page_main", vals)

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=False, methods=["POST"])
    def can_load(self):
        """JS probe. Public, but only returns a boolean."""
        try:
            show = _can_show_widget(request.env)
            _logger.info(
                "[website_ai_chat_min] can_load show=%s user_public=%s",
                show,
                request.env.user._is_public() if request.env.user else True,
            )
            return {"show": bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        """
        Main chat endpoint. Login required + CSRF.
        Enforces rate-limits and (optional) group gate.
        """
        # Optional group gate
        if not _require_group_if_configured(request.env):
            raise AccessDenied(_("You do not have access to AI Chat."))

        # Per-session throttle
        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        # Normalize input
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        # Privacy-safe structured log (no raw question, no PII)
        try:
            _logger.info(
                "[website_ai_chat_min] /ai_chat/send uid=%s login_hash=%s len=%s ip=%s",
                request.env.uid,
                tools.compute_hash(request.env.user.login or "n/a"),
                len(q),
                _client_ip(),
            )
        except Exception:
            pass

        try:
            provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs = _get_ai_config()
            if not api_key:
                return {
                    "ok": False,
                    "reply": _("AI provider API key is not configured.\nPlease contact the administrator."),
                }

            # Scope limiter (optional)
            if allowed_regex and not _match_allowed(allowed_regex, q):
                return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

            # Optional document grounding
            context_snippets = []
            try:
                if docs_folder and os.path.isdir(docs_folder):
                    context_snippets = _read_pdf_snippets(docs_folder, q)
            except Exception as e:
                _logger.warning("PDF scan failed: %s", tools.ustr(e))

            context_text = ""
            if context_snippets:
                context_text = _("\nRelevant excerpts:\n") + "\n---\n".join(context_snippets)

            prompt_system = (system_prompt or "")
            if context_text:
                prompt_system = f"{prompt_system}\n{context_text}"

            # Provider call
            try:
                if provider == "gemini":
                    reply = _call_gemini(api_key, model, prompt_system, q)
                else:
                    reply = _call_openai(api_key, model, prompt_system, q)
            except Exception as e:
                _logger.error(
                    "AI provider error (provider=%s, model=%s): %s",
                    provider, model or "default", tools.ustr(e), exc_info=True
                )
                return {"ok": False, "reply": _("The AI service is temporarily unavailable.\nPlease try again shortly.")}

            reply = (reply or "").strip()
            if only_docs and context_text and not reply:
                reply = _("I don’t know based on the current documents.")
            elif only_docs and not context_text:
                reply = _("I don’t know based on the current documents.")

            return {"ok": True, "reply": reply or _("(No answer returned.)")}

        except Exception as e:
            _logger.error("Unexpected server error in /ai_chat/send: %s", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("An unexpected error occurred.\nPlease try again later.")}
        else:
            # Hook for future metrics
            pass
        finally:
            pass
