# -*- coding: utf-8 -*-
from __future__ import annotations

from odoo import http, tools, _
from odoo.http import request

import json
import time
import re as re_std
import logging
from typing import Dict, List, Tuple, Optional, Callable, Any

_logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Caching layer
_QA_CACHE: Dict[str, Dict[str, object]] = {}

# -----------------------------------------------------------------------------
# In-memory rate limit (per IP)
_RATE_BUCKETS: Dict[str, List[float]] = {}
RATE_WINDOW_SECS = 15
RATE_MAX_CALLS = 4


def _client_ip() -> str:
    try:
        return request.httprequest.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
            request.httprequest.remote_addr or "0.0.0.0"
    except Exception:
        return "0.0.0.0"


def _throttle() -> bool:
    """Token-bucket style throttle per client IP."""
    now = time.time()
    ip = _client_ip()
    bucket = _RATE_BUCKETS.setdefault(ip, [])
    cutoff = now - RATE_WINDOW_SECS
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= RATE_MAX_CALLS:
        return False
    bucket.append(now)
    return True


# -----------------------------------------------------------------------------
# Config access
def _icp():
    return request.env["ir.config_parameter"].sudo()


def _get_icp_param(name: str, default: str = "") -> str:
    try:
        return _icp().get_param(name, default) or default
    except Exception:
        return default

# -----------------------------------------------------------------------------
# Store helpers (normalize + fetch from ICP)
def _normalize_store(name: str) -> str:
    """
    Ensure we always use a fully-qualified File Search Store name.
    Valid form is: fileSearchStores/<id>
    """
    name = (name or "").strip()
    return name if (not name or name.startswith("fileSearchStores/")) else f"fileSearchStores/{name}"

# -----------------------------------------------------------------------------
# Allowed-scope regex (admin-controlled)
def _match_allowed(pattern: str, text: str, timeout_ms: int = 120) -> bool:
    """Return True if text matches admin regex. Fail-closed if regex library missing."""
    if not pattern:
        return True
    try:
        try:
            import regex as regex_safe
        except Exception:
            regex_safe = None
        if regex_safe:
            return bool(regex_safe.search(pattern, text, flags=regex_safe.I | regex_safe.M, timeout=timeout_ms))
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Optional PII redaction
def _redact_pii(text: str) -> str:
    try:
        if not text:
            return text
        text = re_std.sub(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"***@***", text)
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)  # phones
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)  # simple IDs
        return text
    except Exception:
        return text


# -----------------------------------------------------------------------------
# Prompt composition
def _build_system_preamble(system_prompt: str, snippets: List[Tuple[str, int, str]]) -> str:
    """Build the final system message (today: just the configured system prompt)."""
    lines: List[str] = []
    base = (system_prompt or "").strip()
    if base:
        lines.append(base)
    else:
        lines.append("Be concise and helpful. Use markdown when formatting lists or steps. Your reply should be in human-readable format.")
    return "\n\n".join(lines)


# -----------------------------------------------------------------------------
# Provider base + adapters (OpenAI / Gemini)
class _ProviderBase:
    def __init__(self, api_key: str, model: str, timeout: int, temperature: float, max_tokens: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    def ask(self, system_text: str, user_text: str) -> str:
        raise NotImplementedError

    def _with_retries(self, fn: Callable[[], str], tries: int = 2) -> str:
        last = None
        for _ in range(max(1, tries)):
            try:
                return fn()
            except Exception as e:
                last = e
                time.sleep(0.4)
        raise last or RuntimeError("provider failed")


class _OpenAIProvider(_ProviderBase):
    def ask(self, system_text: str, user_text: str) -> str:
        try:
            import openai
        except Exception:
            return "The OpenAI client library is not installed on the server."
        openai.api_key = self.api_key

        timeout_ms = self.timeout * 1000 if self.timeout < 1000 else self.timeout

        def _call() -> str:
            try:
                resp = openai.ChatCompletion.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    messages=[
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": user_text},
                    ],
                    request_timeout=timeout_ms,
                )
                txt = resp["choices"][0]["message"]["content"].strip()
                return txt
            except AttributeError:
                client = openai.OpenAI(api_key=self.api_key)
                r = client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    messages=[
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": user_text},
                    ],
                    timeout=timeout_ms,
                )
                return (r.choices[0].message.content or "").strip()

        return self._with_retries(_call)


class _GeminiProvider(_ProviderBase):
    def __init__(self, *args, file_store_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        # strip to avoid accidental whitespace in store names
        self.file_store_id = (file_store_id or "").strip()

    def ask(self, system_text: str, user_text: str) -> str:
        timeout_ms = self.timeout * 1000 if self.timeout < 1000 else self.timeout
        try:
            from google import genai
            from google.genai import types
            import httpx
        except Exception:
            return "The Gemini client library is not installed on the server."

        # Build tools/config once
        tools = None
        if self.file_store_id:
            tools = [
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[self.file_store_id]
                    )
                )
            ]
        cfg = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            tools=tools,
            system_instruction=system_text or "",
        )

        # Three httpx clients to try in order:
        clients = []

        # 1) Ignore env proxies/CA, force IPv4, HTTP/1.1
        try:
            transport_ipv4 = httpx.HTTPTransport(local_address="0.0.0.0")  # force IPv4
            clients.append((
                "noenv-ipv4-h1",
                httpx.Client(trust_env=False, http2=False, transport=transport_ipv4, timeout=timeout_ms)
            # ignore env
            ))
        except Exception as e:
            _logger.warning("Gemini httpx transport build failed (ipv4): %s", e)

        # 2) Respect env proxies (if corp proxy is required), still IPv4, HTTP/1.1
        try:
            transport_ipv4_b = httpx.HTTPTransport(local_address="0.0.0.0")
            clients.append((
                "env-ipv4-h1",
                httpx.Client(trust_env=True, http2=False, transport=transport_ipv4_b, timeout=timeout_ms)
            ))
        except Exception as e:
            _logger.warning("Gemini httpx transport build failed (env-ipv4): %s", e)

        # 3) Ignore env, default route, HTTP/1.1
        clients.append((
            "noenv-default-h1",
            httpx.Client(trust_env=False, http2=False, timeout=timeout_ms)
        ))

        last_exc = None
        for label, hclient in clients:
            try:
                # Optional preflight to surface handshake issues with exactly this client
                try:
                    hclient.head("https://generativelanguage.googleapis.com",
                                 timeout=10)  # 404 is fine; handshake must complete
                except Exception as pre:
                    _logger.warning("Gemini preflight (%s) failed: %s", label, pre)
                    raise

                client = genai.Client(
                    api_key=self.api_key or None,
                    http_options=types.HttpOptions(
                        timeout=timeout_ms,
                        httpx_client=hclient,  # SDK uses this httpx client for all calls
                    ),
                )

                r = client.models.generate_content(
                    model=self.model,
                    contents=user_text,
                    config=cfg,
                )
                return (getattr(r, "text", None) or "").strip()

            except Exception as e:
                last_exc = e
                _logger.error("Gemini attempt %s failed: %s", label, e, exc_info=True)
            finally:
                try:
                    hclient.close()
                except Exception:
                    pass

        # If all attempts failed, return a clear message for the UI
        return f"Error during Gemini request: {last_exc}"


def _get_provider(cfg: Dict[str, Any]) -> _ProviderBase:
    if (cfg["provider"] or "").strip().lower() == "gemini":
        return _GeminiProvider(
            cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"],
            file_store_id=cfg.get("file_store_id", ""),
        )
    return _OpenAIProvider(cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"])


# -----------------------------------------------------------------------------
# Config loader
AI_DEFAULT_TIMEOUT = 15
AI_DEFAULT_TEMPERATURE = 0.2
AI_DEFAULT_MAX_TOKENS = 512


def _get_ai_config() -> Dict[str, Any]:
    provider = _get_icp_param("website_ai_chat_min.ai_provider", "gemini")
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    model = _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "")
    docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "")

    file_search_enabled = _bool_icp("website_ai_chat_min.file_search_enabled", False)
    # NEW: prefer file_store_id, fallback to file_store_id; normalize to fully-qualified form
    file_store_id = _normalize_store(_get_icp_param("website_ai_chat_min.file_store_id", ""))

    file_search_index = _get_icp_param("website_ai_chat_min.file_search_index", "")
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
    redact_pii = _bool_icp("website_ai_chat_min.redact_pii", False)

    temperature = 0.3
    max_tokens = 1536
    timeout = 60

    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "system_prompt": system_prompt,
        "docs_folder": docs_folder,
        "file_search_enabled": file_search_enabled,
        "file_store_id": file_store_id,  # <- fully-qualified store goes here
        "file_search_index": file_search_index,
        "allowed_regex": allowed_regex,
        "redact_pii": redact_pii,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }


# -----------------------------------------------------------------------------
# Request parsing (accepts {question} or JSON-RPC)
def _normalize_message_from_request(question_param: Optional[str] = None) -> str:
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

# -----------------------------------------------------------------------------
# Lightweight per-user memory in Odoo session (no DB changes)

_SESSION_MEM_KEY = "ai_chat_history_v1"

def _mem_bucket_key(cfg: Dict[str, Any]) -> str:
    # isolate memory per provider/model/store
    return f"{(cfg.get('provider') or '').strip()}::{(cfg.get('model') or '').strip()}::{(cfg.get('file_store_id') or '').strip()}"

def _mem_load(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    sess = getattr(request, "session", None)
    if not sess:
        return []
    bucket = sess.get(_SESSION_MEM_KEY) or {}
    return list(bucket.get(_mem_bucket_key(cfg)) or [])

def _mem_save(cfg: Dict[str, Any], history: List[Dict[str, Any]]) -> None:
    sess = getattr(request, "session", None)
    if not sess:
        return
    bucket = dict(sess.get(_SESSION_MEM_KEY) or {})
    bucket[_mem_bucket_key(cfg)] = history
    sess[_SESSION_MEM_KEY] = bucket
    try:
        sess.modified = True  # ensure persistence
    except Exception:
        pass

def _mem_append(cfg: Dict[str, Any], role: str, text: str, max_msgs: int = 30, max_chars: int = 24000) -> None:
    """Append a turn and trim for context window."""
    h = _mem_load(cfg)
    h.append({"role": role, "parts": [{"text": (text or "")[:8000]}]})
    if len(h) > max_msgs:
        h = h[-max_msgs:]
    total = 0
    trimmed = []
    for m in reversed(h):
        part = (m.get("parts") or [{}])[0].get("text") or ""
        total += len(part)
        trimmed.append(m)
        if total >= max_chars:
            break
    _mem_save(cfg, list(reversed(trimmed)))

def _mem_contents(cfg: Dict[str, Any], system_text: str = "") -> List[Dict[str, Any]]:
    """Gemini has no 'system' role; include system preamble as first user part."""
    contents: List[Dict[str, Any]] = []
    if (system_text or "").strip():
        contents.append({"role": "user", "parts": [{"text": system_text.strip()}]})
    contents.extend(_mem_load(cfg))
    return contents



# -----------------------------------------------------------------------------
# Controller
class AiChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="user", csrf=True, methods=["POST"])
    def can_load(self):
        """
        Determines whether the AI chat widget should be mounted for the current
        user. Only users belonging to the 'Website AI Chat / User' or
        'Website AI Chat / Admin' groups are allowed to see the chat bubble.
        """
        try:
            user = request.env.user
            has_user_group = user.has_group('website_ai_chat_min.group_ai_chat_user')
            has_admin_group = user.has_group('website_ai_chat_min.group_ai_chat_admin')
            allowed = bool(has_user_group or has_admin_group)
            return {"show": allowed}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        """
        Validates, composes prompt, calls provider with retries,
        and returns a compact, structured reply.

        Optional request override:
          { "message": "...", "store": "fileSearchStores/xyz" }
        """
        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        # Extract payload
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        cfg = _get_ai_config()
        if not cfg["api_key"]:
            return {"ok": False, "reply": _("AI provider API key is not configured. Please contact the administrator.")}

        # Optional: per-request store override
        override_store = ""
        try:
            payload = request.jsonrequest or {}
            if isinstance(payload, dict):
                override_store = _normalize_store((payload.get("store") or "").strip())
        except Exception:
            override_store = ""
        if override_store:
            cfg["file_store_id"] = override_store

        # Respect allow-list (optional)
        if cfg["allowed_regex"] and not _match_allowed(cfg["allowed_regex"], q):
            return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # Cache lookup (use redacted text as the key if redaction is enabled)
        cache_key = outbound_q
        cached = _QA_CACHE.get(cache_key)
        if cached:
            ui = dict(cached["ui"])
            ui.setdefault("ai_status", {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "store": cfg["file_store_id"] if cfg["file_search_enabled"] else None,
            })
            return {"ok": True, "reply": cached["reply"], "ui": ui}

        # Compose system prompt
        system_text = _build_system_preamble(cfg["system_prompt"], [])

        # If File Search isn't enabled, ensure we don't attach a store
        effective_store = cfg["file_store_id"] if cfg["file_search_enabled"] else ""
        cfg["file_store_id"] = effective_store

        # ── MEMORY: append user turn, build contents, call, append model turn ─────
        provider = _get_provider(cfg)
        try:
            # 1) remember the new user turn
            _mem_append(cfg, "user", outbound_q)

            # 2) compose multi-turn contents (system preamble + history)
            contents = _mem_contents(cfg, system_text)

            # 3) ask with the full contents (Gemini SDK accepts list-of-messages)
            answer_text = provider.ask(system_text, contents).strip()

            # 4) remember the model's reply
            _mem_append(cfg, "model", answer_text)
        except Exception as e:
            _logger.error("provider call failed: %s", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("Network or provider error. Please try again.")}

        # Shape minimal UI (now includes ai_status so the frontend can show the active store)
        ui = {
            "title": "",
            "summary": "",
            "answer_md": answer_text[:1800] if answer_text else "",
            "citations": [],
            "suggestions": [],
            "ai_status": {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "store": effective_store or None,
            },
        }

        # Cache and return
        _QA_CACHE[cache_key] = {"reply": ui["answer_md"], "ui": dict(ui)}
        return {"ok": True, "reply": (ui["answer_md"] or _("(No answer returned.)")), "ui": ui}

