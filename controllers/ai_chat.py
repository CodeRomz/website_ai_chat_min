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


def _int_icp(name: str, default: int) -> int:
    try:
        v = _get_icp_param(name, str(default))
        return int(v)
    except Exception:
        return default


def _float_icp(name: str, default: float) -> float:
    try:
        v = _get_icp_param(name, str(default))
        return float(v)
    except Exception:
        return default


def _bool_icp(name: str, default: bool) -> bool:
    try:
        v = (_get_icp_param(name, "1" if default else "0") or "").strip().lower()
        return v in ("1", "true", "yes", "y", "on")
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


def _get_store_from_icp() -> str:
    """
    Prefer the dedicated file_store_id if present, else fallback to the older field.
    """
    s1 = _get_icp_param("website_ai_chat_min.file_store_id", "")
    s2 = _get_icp_param("website_ai_chat_min.file_search_store", "")
    return _normalize_store(s1 or s2)


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
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)            # phones
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)            # simple IDs
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
        lines.append("Be concise and helpful. Use markdown when formatting lists or steps.")
    return "\n".join(lines)


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
                    request_timeout=self.timeout,
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
                    timeout=self.timeout,
                )
                return (r.choices[0].message.content or "").strip()

        return self._with_retries(_call)


class _GeminiProvider(_ProviderBase):
    def __init__(self, *a, file_search_store: str = "", **kw):
        super().__init__(*a, **kw)
        self.file_search_store = (file_search_store or "").strip()

    def ask(self, system_text: str, user_text: str) -> str:
        try:
            # New Google GenAI SDK
            from google import genai
            from google.genai import types
        except Exception:
            return "The Gemini client library is not installed on the server."

        client = genai.Client(api_key=self.api_key or None)

        # Attach File Search only when a store name is configured
        tools = None
        if self.file_search_store:
            tools = [
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[self.file_search_store]
                    )
                )
            ]

        cfg = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            tools=tools,
            system_instruction=system_text or "",
        )

        r = client.models.generate_content(
            model=self.model,
            contents=user_text,
            config=cfg,
            request_options={"timeout": self.timeout},
        )
        return (getattr(r, "text", None) or "").strip()


def _get_provider(cfg: Dict[str, Any]) -> _ProviderBase:
    if cfg["provider"] == "gemini":
        return _GeminiProvider(
            cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"],
            file_search_store=cfg.get("file_search_store", ""),
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
    # NEW: prefer file_store_id, fallback to file_search_store; normalize to fully-qualified form
    normalized_store = _get_store_from_icp()

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
        "file_search_store": normalized_store,   # <- fully-qualified store goes here
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
# Controller
class AiChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="user", csrf=True, methods=["POST"])
    def can_load(self):
        """Login gate for mounting the widget; returns a minimal boolean."""
        try:
            return {"show": True}
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
            cfg["file_search_store"] = override_store

        # Respect allow-list (optional)
        if cfg["allowed_regex"] and not _match_allowed(cfg["allowed_regex"], q):
            return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # Cache lookup (use redacted text as the key if redaction is enabled)
        cache_key = outbound_q
        cached = _QA_CACHE.get(cache_key)
        if cached:
            # decorate cached UI with current ai_status as a convenience
            ui = dict(cached["ui"])
            ui.setdefault("ai_status", {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "store": cfg["file_search_store"] if cfg["file_search_enabled"] else None,
            })
            return {"ok": True, "reply": cached["reply"], "ui": ui}

        # Compose system prompt
        system_text = _build_system_preamble(cfg["system_prompt"], [])

        # If File Search isn't enabled, ensure we don't attach a store
        effective_store = cfg["file_search_store"] if cfg["file_search_enabled"] else ""
        cfg["file_search_store"] = effective_store

        # Call provider
        provider = _get_provider(cfg)
        try:
            answer_text = provider.ask(system_text, outbound_q).strip()
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
