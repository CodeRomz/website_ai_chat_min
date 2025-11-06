# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import json
import time
import re as re_std
import os

# Optional safer regex (supports timeouts)
try:
    import regex as regex_safe  # type: ignore
except Exception:
    regex_safe = None

# Default rate-limits (override via config parameters)
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15  # seconds


# ---------------------------------------------------------------------------
# Authorization & Config Helpers
# ---------------------------------------------------------------------------

def _user_can_use_chat(env):
    """Return True if current user can access the chat widget/pages."""
    user = env.user
    return bool(
        user and not user._is_public() and (
            user.has_group('website_ai_chat_min.group_ai_chat_user') or
            user.has_group('base.group_system')
        )
    )


def _require_sysadmin_or_chat_user(env):
    """Stricter gate for test endpoints."""
    user = env.user
    if not user or user._is_public():
        raise AccessDenied(_("You do not have access."))
    if not (user.has_group('website_ai_chat_min.group_ai_chat_user') or user.has_group('base.group_system')):
        raise AccessDenied(_("You do not have access."))


def _get_icp_param(name, default=''):
    ICP = request.env['ir.config_parameter'].sudo()
    try:
        val = ICP.get_param(name, default) or default
        return val
    except Exception as e:
        _logger.warning("ICP get_param failed for %s: %s", name, tools.ustr(e))
        return default


def _get_ai_config():
    """Fetch AI configuration with env-var overrides."""
    provider = os.getenv('AI_PROVIDER') or _get_icp_param('website_ai_chat_min.ai_provider', 'openai')
    api_key = os.getenv('AI_API_KEY') or _get_icp_param('website_ai_chat_min.ai_api_key', '')
    model = os.getenv('AI_MODEL') or _get_icp_param('website_ai_chat_min.ai_model', '')
    system_prompt = _get_icp_param('website_ai_chat_min.system_prompt', '') or ''
    allowed_regex = _get_icp_param('website_ai_chat_min.allowed_regex', '') or ''
    docs_folder = _get_icp_param('website_ai_chat_min.docs_folder', '') or ''
    only_docs = tools.str2bool(_get_icp_param('website_ai_chat_min.answer_only_from_docs', '0'))
    return provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

def _get_rate_limits():
    """Read rate-limit from system parameters, with sane defaults."""
    ICP = request.env['ir.config_parameter'].sudo()
    try:
        max_req = int(ICP.get_param('website_ai_chat_min.rate_limit_max', DEFAULT_RATE_LIMIT_MAX))
    except Exception:
        max_req = DEFAULT_RATE_LIMIT_MAX
    try:
        window = int(ICP.get_param('website_ai_chat_min.rate_limit_window', DEFAULT_RATE_LIMIT_WINDOW))
    except Exception:
        window = DEFAULT_RATE_LIMIT_WINDOW
    return max(1, max_req), max(1, window)


def _client_ip():
    """Best-effort client IP, honoring X-Forwarded-For when behind a trusted proxy."""
    try:
        xfwd = request.httprequest.headers.get('X-Forwarded-For', '')
        ip = (xfwd.split(',')[0].strip() if xfwd else request.httprequest.remote_addr) or '0.0.0.0'
        return ip
    except Exception:
        return '0.0.0.0'


def _throttle():
    """
    Per-session + per-user/IP throttle in Werkzeug session.
    NOTE: For multi-node, consider Redis-based rate limiter.
    """
    try:
        max_req, window = _get_rate_limits()
        now = time.time()
        user_id = request.env.uid or 0
        ip = _client_ip()
        key = f'website_ai_chat_min_rl:{user_id}:{ip}'
        hist = request.session.get(key, [])
        # Keep only recent timestamps
        hist = [t for t in hist if now - t < window]
        allowed = len(hist) < max_req
        if allowed:
            hist.append(now)
            # Bound the list to max_req to prevent growth
            if len(hist) > max_req:
                hist = hist[-max_req:]
            request.session[key] = hist
            request.session.modified = True
        return allowed
    except Exception as e:
        # Fail-open but log server-side; do not block user
        _logger.warning("Throttle error: %s", tools.ustr(e))
        return True


# ---------------------------------------------------------------------------
# Input Normalization
# ---------------------------------------------------------------------------

def _get_incoming_payload():
    """Return parsed JSON body (both JSON-RPC and plain JSON), or {}."""
    try:
        raw = request.httprequest.get_data(cache=False, as_text=True)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _normalize_message_from_request(question_param=None):
    """
    Normalize incoming user text across:
      - JSON-RPC: {"params": {"message": "..."}}
      - Plain JSON: {"message": "..."} or {"question": "..."}
      - Direct kwarg: question=...
    Returns a stripped string (can be empty).
    """
    # 1) Direct kwarg (JSON-RPC kw or JSON kw)
    msg = (question_param or "").strip()
    if msg:
        return msg

    # 2) Raw payload
    payload = _get_incoming_payload()
    if isinstance(payload, dict):
        # JSON-RPC path first
        params = payload.get('params')
        if isinstance(params, dict):
            msg = (params.get('message') or params.get('question') or "").strip()
            if msg:
                return msg
        # Plain JSON fallback
        msg = (payload.get('message') or payload.get('question') or "").strip()
        if msg:
            return msg

    return ""


# ---------------------------------------------------------------------------
# Regex Safety
# ---------------------------------------------------------------------------

def _match_allowed(pattern, text, timeout_ms=50):
    """Time-boxed regex check using 'regex' if available, else stdlib fallback."""
    if not pattern:
        return True
    try:
        if regex_safe:
            return bool(regex_safe.search(pattern, text, flags=regex_safe.I | regex_safe.M, timeout=timeout_ms))
        # Fallback without timeout — mitigated by 4k length cap and try/except
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception as e:
        _logger.warning("Invalid allowed_regex or match error: %s", tools.ustr(e))
        return False  # safer default


# ---------------------------------------------------------------------------
# PDF Context (best-effort, time-capped)
# ---------------------------------------------------------------------------

def _read_pdf_snippets(root_folder, query, max_files=40, max_pages=40, per_page_chars=1200,
                       max_hits=12, max_runtime_ms=350):
    """Small keyword-based extraction with resource caps and wall-clock ceiling."""
    try:
        import pypdf  # type: ignore
    except Exception as e:
        _logger.info("pypdf not installed: %s", tools.ustr(e))
        return []

    start = time.time()
    hits = []
    ql = query.lower()
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith('.pdf'):
                continue
            if len(hits) >= max_hits:
                return hits
            if max_files <= 0:
                return hits
            if (time.time() - start) * 1000 > max_runtime_ms:
                _logger.info("PDF scan aborted due to time ceiling (%sms)", max_runtime_ms)
                return hits
            max_files -= 1
            path = os.path.join(dirpath, fn)
            try:
                with open(path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    for i, page in enumerate(reader.pages[:max_pages]):
                        text = (page.extract_text() or '')[:per_page_chars]
                        if ql in text.lower():
                            hits.append(f"[{fn} p.{i+1}] {text.strip()}")
                            if len(hits) >= max_hits:
                                return hits
                        if (time.time() - start) * 1000 > max_runtime_ms:
                            _logger.info("PDF scan aborted mid-file due to time ceiling")
                            return hits
            except Exception as e:
                _logger.warning("Failed reading PDF %s: %s", path, tools.ustr(e))
    return hits


# ---------------------------------------------------------------------------
# AI Provider Calls
# ---------------------------------------------------------------------------

def _call_openai(api_key, model, system_prompt, user_text):
    """OpenAI Chat Completions with strict timeouts and token caps."""
    from openai import OpenAI  # new SDK
    # Prefer per-client timeout; if unsupported in your SDK version, wrap with network timeouts externally.
    client = OpenAI(api_key=api_key, timeout=15)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})
    resp = client.chat.completions.create(
        model=(model or "gpt-4o-mini"),
        messages=messages,
        max_tokens=512,
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def _call_gemini(api_key, model, system_prompt, user_text):
    """Gemini API text generation with timeout and token caps."""
    import google.generativeai as genai  # type: ignore
    genai.configure(api_key=api_key)
    model_name = model or "gemini-2.5-flash"
    prompt = (system_prompt + "\n\n" if system_prompt else "") + user_text
    r = genai.GenerativeModel(model_name).generate_content(
        [prompt],
        request_options={"timeout": 15},
        generation_config={"temperature": 0.2, "max_output_tokens": 512},
    )
    return (getattr(r, "text", None) or "").strip()


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------

class WebsiteAIChatController(http.Controller):

    @http.route('/ai-chat', type='http', auth='user', website=True, methods=['GET'])
    def ai_chat_page(self, **kw):
        if not _user_can_use_chat(request.env):
            raise AccessError(_("You do not have access to AI Chat."))
        vals = {
            'privacy_url': request.env['ir.config_parameter'].sudo().get_param(
                'website_ai_chat_min.privacy_url', default=''
            ),
        }
        return request.render('website_ai_chat_min.ai_chat_page_main', vals)

    @http.route('/ai_chat/can_load', type='json', auth='public', csrf=False, methods=['POST'])
    def can_load(self):
        try:
            show = _user_can_use_chat(request.env)
            _logger.info("[website_ai_chat_min] can_load show=%s user_hash=%s",
                         show, tools.compute_hash(request.env.user.login or 'n/a'))
            return {'show': bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {'show': False}

    @http.route('/ai_chat/send', type='json', auth='user', csrf=True, methods=['POST'])
    def send(self, question=None):
        """
        Main chat endpoint. Enforces group checks, CSRF, rate-limits, and GDPR logging hygiene.
        Now accepts both JSON-RPC and plain JSON payloads (mirrors *_test normalization).
        """
        # Authorization
        if not _user_can_use_chat(request.env):
            raise AccessError(_("You do not have access to AI Chat."))

        # Per-session throttle
        if not _throttle():
            return {'ok': False, 'reply': _("Please wait a moment before sending another message.")}

        # Normalize & validate input
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {'ok': False, 'reply': _("Please enter a question.")}
        if len(q) > 4000:
            return {'ok': False, 'reply': _("Question too long (max 4000 chars).")}

        uid = request.env.uid
        login = request.env.user.login or 'n/a'

        # Structured log (GDPR-safe; do not log raw question)
        try:
            _logger.info(
                "[website_ai_chat_min] /ai_chat/send uid=%s login_hash=%s len=%s ip=%s",
                uid, tools.compute_hash(login), len(q), _client_ip()
            )
        except Exception:
            pass

        try:
            provider, api_key, model, system_prompt, allowed_regex, docs_folder, only_docs = _get_ai_config()

            if not api_key:
                return {'ok': False, 'reply': _("AI provider API key is not configured. Please contact the administrator.")}

            # Allowed scope (regex)
            if allowed_regex and not _match_allowed(allowed_regex, q):
                return {'ok': False, 'reply': _("Your question is not within the allowed scope.")}

            # Optional PDF context (best effort, with caps)
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

            # Provider call with robust exception handling
            try:
                if provider == 'gemini':
                    reply = _call_gemini(api_key, model, prompt_system, q)
                else:
                    reply = _call_openai(api_key, model, prompt_system, q)
            except Exception as e:
                _logger.error("AI provider error (provider=%s, model=%s): %s",
                              provider, model or 'default', tools.ustr(e), exc_info=True)
                return {'ok': False, 'reply': _("The AI service is temporarily unavailable. Please try again shortly.")}

            reply = (reply or "").strip()
            if only_docs and context_text and not reply:
                reply = _("I don’t know based on the current documents.")
            elif only_docs and not context_text:
                reply = _("I don’t know based on the current documents.")

            return {'ok': True, 'reply': reply or _("(No answer returned.)")}

        except Exception as e:
            _logger.error("Unexpected server error in /ai_chat/send uid=%s login=%s: %s",
                          uid, login, tools.ustr(e), exc_info=True)
            return {'ok': False, 'reply': _("An unexpected error occurred. Please try again later.")}
        else:
            # Hook for future metrics
            pass
        finally:
            # Explicit for readability
            pass
