# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import time
import re
import os

# Default rate-limits (override via config parameters)
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15  # seconds


def _user_can_use_chat(env):
    """Return True if current user can access the chat widget/pages."""
    user = env.user
    return bool(
        user and not user._is_public() and (
            user.has_group('website_ai_chat_min.group_ai_chat_user') or
            user.has_group('base.group_system')
        )
    )


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


def _throttle():
    """
    Simple per-session throttle stored in Werkzeug session.
    Use Redis/memcached for distributed setups.
    """
    try:
        max_req, window = _get_rate_limits()
        now = time.time()
        key = 'website_ai_chat_min_rl'
        hist = request.session.get(key, [])
        hist = [t for t in hist if now - t < window]
        allowed = len(hist) < max_req
        if allowed:
            hist.append(now)
            request.session[key] = hist
            request.session.modified = True
        return allowed
    except Exception as e:
        # Fail-open but log server-side; do not block user
        _logger.warning("Throttle error: %s", tools.ustr(e))
        return True


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
            _logger.info("[website_ai_chat_min] can_load show=%s user=%s",
                         show, request.env.user.login)
            return {'show': bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {'show': False}

    @http.route('/ai_chat/send', type='json', auth='user', csrf=True, methods=['POST'])
    def send(self, question=None):
        """Main chat endpoint. Enforces group checks, CSRF, rate-limits, and GDPR logging hygiene."""
        # Authorization
        if not _user_can_use_chat(request.env):
            raise AccessError(_("You do not have access to AI Chat."))

        # Per-session throttle
        if not _throttle():
            return {'ok': False, 'reply': _("Please wait a moment before sending another message.")}

        # Validate and sanitize
        q = (question or "").strip()
        if not q:
            return {'ok': False, 'reply': _("Please enter a question.")}
        if len(q) > 4000:
            return {'ok': False, 'reply': _("Question too long (max 4000 chars).")}

        ICP = request.env['ir.config_parameter'].sudo()
        uid = request.env.uid
        login = request.env.user.login or 'n/a'

        # Structured log (GDPR-safe; do not log raw question)
        _logger.info("[website_ai_chat_min] /ai_chat/send uid=%s login=%s len=%s", uid, login, len(q))

        try:
            provider = ICP.get_param('website_ai_chat_min.ai_provider', default='openai')
            api_key = ICP.get_param('website_ai_chat_min.ai_api_key', default='') or ''
            model   = ICP.get_param('website_ai_chat_min.ai_model', default='')
            system_prompt = ICP.get_param('website_ai_chat_min.system_prompt', default='') or ''
            allowed_regex = ICP.get_param('website_ai_chat_min.allowed_regex', default='') or ''
            docs_folder = ICP.get_param('website_ai_chat_min.docs_folder', default='') or ''
            only_docs   = tools.str2bool(ICP.get_param('website_ai_chat_min.answer_only_from_docs', default='0'))

            if not api_key:
                return {'ok': False, 'reply': _("AI provider API key is not configured. Please contact the administrator.")}

            # Allowed scope (regex)
            if allowed_regex:
                try:
                    if not re.search(allowed_regex, q, flags=re.I | re.M):
                        return {'ok': False, 'reply': _("Your question is not within the allowed scope.")}
                except Exception as e:
                    _logger.warning("Invalid allowed_regex: %s", tools.ustr(e))

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
                return {'ok': False, 'reply': _("AI provider error: %s") % tools.ustr(e)}

            reply = (reply or "").strip()
            if only_docs and context_text and not reply:
                reply = _("I don’t know based on the current documents.")
            elif only_docs and not context_text:
                reply = _("I don’t know based on the current documents.")

            return {'ok': True, 'reply': reply or _("(No answer returned.)")}

        except Exception as e:
            _logger.error("Unexpected server error in /ai_chat/send uid=%s login=%s: %s",
                          uid, login, tools.ustr(e), exc_info=True)
            return {'ok': False, 'reply': _("Unexpected error: %s") % tools.ustr(e)}
        else:
            # Hook for future metrics
            pass
        finally:
            # Explicit for readability
            pass


def _read_pdf_snippets(root_folder, query, max_files=40, max_pages=40, per_page_chars=1200, max_hits=12):
    """Small keyword-based extraction with resource caps."""
    try:
        import pypdf  # type: ignore
    except Exception as e:
        _logger.info("pypdf not installed: %s", tools.ustr(e))
        return []

    hits = []
    ql = query.lower()
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith('.pdf'):
                continue
            if len(hits) >= max_hits or max_files <= 0:
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
            except Exception as e:
                _logger.warning("Failed reading PDF %s: %s", path, tools.ustr(e))
    return hits


def _call_openai(api_key, model, system_prompt, user_text):
    """OpenAI Chat Completions (per OpenAI docs)."""
    try:
        from openai import OpenAI  # new SDK
        client = OpenAI(api_key=api_key)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
        resp = client.chat.completions.create(model=(model or "gpt-4o-mini"), messages=messages)
        return (resp.choices[0].message.content or "").strip()
    except ImportError:
        import openai  # legacy SDK
        openai.api_key = api_key
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
        resp = openai.ChatCompletion.create(model=(model or "gpt-3.5-turbo"), messages=messages)
        return (resp.choices[0]['message']['content'] or "").strip()


def _call_gemini(api_key, model, system_prompt, user_text):
    """Gemini API text generation (per Google docs)."""
    import google.generativeai as genai  # type: ignore
    genai.configure(api_key=api_key)
    model_name = model or "gemini-2.5-flash"
    sys = system_prompt or ""
    content = f"{sys}\n\nUser: {user_text}".strip()
    r = genai.GenerativeModel(model_name).generate_content([content])
    return (getattr(r, "text", None) or "").strip()



class WebsiteAIChatTestController(http.Controller):
    """Minimal test page + JSON endpoint to verify FE <-> BE connectivity and (optional) AI call."""

    @http.route('/ai-chat-test', type='http', auth='user', website=True, methods=['GET'])
    def ai_chat_test_page(self, **kw):
        """Render a simple page with an input and a Send button."""
        try:
            values = {
                "user_name": request.env.user.name,
            }
        except Exception as e:
            _logger.exception("Failed to prepare AI Chat Test page context")
            # Render a very small error page instead of bubbling a 500
            return request.make_response(
                "<h3>AI Chat Test</h3><p>Server error: %s</p>" % tools.html_escape(str(e)),
                headers=[("Content-Type", "text/html; charset=utf-8")],
            )
        else:
            return request.render('website_ai_chat_min.ai_chat_test_page', values)
        finally:
            pass  # reserved for future cleanup

    @http.route('/ai_chat_test/send', type='json', auth='user', methods=['POST'])
    def ai_chat_test_send(self, message=None):
        """Accept a message, optionally hit AI provider, return JSON. Works even without provider (Echo)."""
        # Basic input normalization
        # msg = (message or "").strip()
        msg = str(message)
        _logger.info("AI-TEST recv user=%s len=%s", request.env.user.id, len(msg))

        try:
            # Pull config (if present)
            icp = request.env['ir.config_parameter'].sudo()
            provider = (icp.get_param('website_ai_chat_min.ai_provider') or '').lower()
            api_key = icp.get_param('website_ai_chat_min.ai_api_key')
            model = icp.get_param('website_ai_chat_min.ai_model') or 'gpt-3.5-turbo'
            system_prompt = icp.get_param('website_ai_chat_min.system_prompt') or ''

            # No input? Short-circuit
            if not msg:
                return {"ok": True, "reply": _("(Nothing to send) Please type a message.")}

            reply = None

            # --- Optional OpenAI ---
            if provider == 'openai' and api_key:
                try:
                    import openai  # type: ignore
                    openai.api_key = api_key
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": msg})
                    resp = openai.ChatCompletion.create(
                        model=model,
                        messages=messages,
                        timeout=15,
                    )
                    reply = (resp.choices[0].message['content'] or '').strip()
                except Exception as oe:
                    _logger.exception("OpenAI call failed")
                    reply = _("(OpenAI error) %s") % str(oe)

            # --- Optional Gemini ---
            elif provider in ('gemini', 'google') and api_key:
                try:
                    import google.generativeai as genai  # type: ignore
                    genai.configure(api_key=api_key)
                    model_name = model or "gemini-1.5-flash"
                    gen_model = genai.GenerativeModel(model_name)
                    # prompt = (system_prompt + "\n\n" if system_prompt else "") + msg

                    prompt = msg
                    r = gen_model.generate_content(prompt, request_options={"timeout": 15})
                    reply = (getattr(r, "text", None) or "").strip()
                except Exception as ge:
                    _logger.exception("Gemini call failed")
                    reply = _("(Gemini error) %s") % str(ge)

            # --- Fallback: Echo ---
            else:
                reply = _("Echo: %s") % msg

            if not reply:
                reply = _("(No reply)")

            return {"ok": True, "reply": reply}

        except Exception as e:
            _logger.exception("ai_chat_test_send failed")
            # Keep user-facing errors generic; log details server-side
            return {"ok": False, "error": _("Server error. Please try again."), "detail": tools.html_escape(str(e))}
        else:
            pass
        finally:
            pass
