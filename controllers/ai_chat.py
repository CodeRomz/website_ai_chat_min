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
# In-memory caches / buckets
_QA_CACHE: Dict[str, Dict[str, object]] = {}

# Simple per-IP rate limit (token bucket)
_RATE_BUCKETS: Dict[str, List[float]] = {}
RATE_WINDOW_SECS = 5
RATE_MAX_CALLS = 1

# Per-user prompt quota (per model, 24h window)
_PROMPT_BUCKETS: Dict[str, List[float]] = {}
PROMPT_WINDOW_SECS = 24 * 60 * 60  # 24 hours


# -----------------------------------------------------------------------------
# Helpers for rate limiting / quotas
def _client_ip() -> str:
    try:
        return (
            request.httprequest.headers.get("X-Forwarded-For", "")
            .split(",")[0]
            .strip()
            or request.httprequest.remote_addr
            or "0.0.0.0"
        )
    except Exception:
        return "0.0.0.0"


def _throttle() -> bool:
    """Token-bucket style throttle per client IP."""
    now = time.time()
    ip = _client_ip()
    bucket = _RATE_BUCKETS.setdefault(ip, [])
    cutoff = now - RATE_WINDOW_SECS
    # Drop entries older than the window
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= RATE_MAX_CALLS:
        return False
    bucket.append(now)
    return True


def _quota_key(user_id: int, model_code: str) -> str:
    """Build a stable per-user/per-model quota key."""
    return f"{user_id}|gemini|{(model_code or '').strip()}"


def _check_user_quota(user_id: int, model_code: str, prompt_limit: int) -> bool:
    """Check and update per-user, per-model prompt quota in a 24h window.

    Returns True if the call is allowed and records the usage, False if the
    quota has been exceeded.

    NOTE: this is in-memory and per Odoo worker. For strict accounting across
    restarts or multiple workers, add a DB-backed log model and migrate this
    logic there.
    """
    if prompt_limit <= 0:
        # 0 or negative means "no per-day limit"
        return True

    now = time.time()
    key = _quota_key(user_id, model_code)
    bucket = _PROMPT_BUCKETS.setdefault(key, [])
    cutoff = now - PROMPT_WINDOW_SECS

    while bucket and bucket[0] < cutoff:
        bucket.pop(0)

    if len(bucket) >= prompt_limit:
        return False

    bucket.append(now)
    return True


# -----------------------------------------------------------------------------
# Config access
def _icp():
    return request.env["ir.config_parameter"].sudo()


def _get_icp_param(name: str, default: Any = "") -> Any:
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
    """Return True if text matches admin regex. Fail-closed on regex errors."""
    if not pattern:
        return True
    try:
        try:
            import regex as regex_safe  # type: ignore[import]
        except Exception:
            regex_safe = None
        if regex_safe:
            return bool(
                regex_safe.search(
                    pattern,
                    text,
                    flags=regex_safe.I | regex_safe.M,
                    timeout=timeout_ms,
                )
            )
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception:
        # If the pattern is broken or times out, deny usage.
        return False


# -----------------------------------------------------------------------------
# PII redaction
def _redact_pii(text: str) -> str:
    """Basic masking for email, phone numbers, and simple IDs."""
    try:
        if not text:
            return text
        # Emails
        text = re_std.sub(
            r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            r"***@***",
            text,
        )
        # Phone numbers
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)
        # Simple IDs (8–12 chars)
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)
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
        lines.append(
            "Be concise and helpful. Use markdown when formatting lists or steps. "
            "Your reply should be in human-readable format."
        )
    # `snippets` is reserved for future document-context features.
    return "\n\n".join(lines)


# -----------------------------------------------------------------------------
# Provider base + Gemini adapter only
class _ProviderBase:
    def __init__(self, api_key: str, model: str, timeout: int, temperature: float, max_tokens: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    def ask(self, system_text: str, user_text: Any) -> str:
        raise NotImplementedError

    def _with_retries(self, fn: Callable[[], str], tries: int = 2) -> str:
        last: Optional[Exception] = None
        for _ in range(max(1, tries)):
            try:
                return fn()
            except Exception as exc:  # pragma: no cover - defensive
                last = exc
                time.sleep(0.4)
        raise last or RuntimeError("provider failed")


class _GeminiProvider(_ProviderBase):
    def __init__(self, *args, file_store_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        # strip to avoid accidental whitespace in store names
        self.file_store_id = (file_store_id or "").strip()

    def ask(self, system_text: str, user_text: Any) -> str:
        timeout_ms = self.timeout * 1000 if self.timeout < 1000 else self.timeout
        try:
            from google import genai
            from google.genai import types
            import httpx
        except Exception:
            return "The Gemini client library is not installed on the server."

        # Build tools/config once
        tools_cfg = None
        if self.file_store_id:
            tools_cfg = [
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[self.file_store_id]
                    )
                )
            ]
        cfg = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            tools=tools_cfg,
            system_instruction=system_text or "",
        )

        # Three httpx clients to try in order
        clients: List[Tuple[str, Any]] = []

        # 1) Ignore env proxies/CA, force IPv4, HTTP/1.1
        try:
            transport_ipv4 = httpx.HTTPTransport(local_address="0.0.0.0")  # force IPv4
            clients.append(
                (
                    "noenv-ipv4-h1",
                    httpx.Client(
                        trust_env=False,
                        http2=False,
                        transport=transport_ipv4,
                        timeout=timeout_ms,
                    ),
                )
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            _logger.warning("Gemini httpx transport build failed (ipv4): %s", exc)

        # 2) Respect env proxies (if corp proxy is required), still IPv4, HTTP/1.1
        try:
            transport_ipv4_b = httpx.HTTPTransport(local_address="0.0.0.0")
            clients.append(
                (
                    "env-ipv4-h1",
                    httpx.Client(
                        trust_env=True,
                        http2=False,
                        transport=transport_ipv4_b,
                        timeout=timeout_ms,
                    ),
                )
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            _logger.warning("Gemini httpx transport build failed (env-ipv4): %s", exc)

        # 3) Ignore env, default route, HTTP/1.1
        clients.append(
            (
                "noenv-default-h1",
                httpx.Client(trust_env=False, http2=False, timeout=timeout_ms),
            )
        )

        last_exc: Optional[Exception] = None
        for label, hclient in clients:
            try:
                # Optional preflight to surface handshake issues with exactly this client
                try:
                    hclient.head(
                        "https://generativelanguage.googleapis.com",
                        timeout=10,
                    )  # 404 is fine; handshake must complete
                except Exception as pre:
                    _logger.warning("Gemini preflight (%s) failed: %s", label, pre)
                    raise

                client = genai.Client(
                    api_key=self.api_key or None,
                    http_options=types.HttpOptions(
                        timeout=timeout_ms,
                        httpx_client=hclient,  # SDK uses this client for all calls
                    ),
                )

                r = client.models.generate_content(
                    model=self.model,
                    contents=user_text,
                    config=cfg,
                )
                return (getattr(r, "text", None) or "").strip()

            except Exception as exc:
                last_exc = exc
                _logger.error("Gemini attempt %s failed: %s", label, exc, exc_info=True)
            finally:
                try:
                    hclient.close()
                except Exception:
                    pass

        # If all attempts failed, return a clear message for the UI
        return f"Error during Gemini request: {last_exc}"


def _get_provider(cfg: Dict[str, Any]) -> _ProviderBase:
    """Single provider: Gemini."""
    return _GeminiProvider(
        cfg["api_key"],
        cfg["model"],
        cfg["timeout"],
        cfg["temperature"],
        cfg["max_tokens"],
        file_store_id=cfg.get("file_store_id", ""),
    )


# -----------------------------------------------------------------------------
# Config loader (Gemini-only)
def _get_ai_config() -> Dict[str, Any]:
    # Provider is conceptually fixed to Gemini now
    provider = "gemini"
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    # The ICP model is kept as a fallback but will be overridden by aic.admin
    model = _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "")
    docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "")

    file_search_enabled = bool(
        tools.strtobool(str(_get_icp_param("website_ai_chat_min.file_search_enabled", False)))
        if hasattr(tools, "strtobool")
        else _get_icp_param("website_ai_chat_min.file_search_enabled", False)
    )
    file_store_id = _normalize_store(_get_icp_param("website_ai_chat_min.file_store_id", ""))

    file_search_index = _get_icp_param("website_ai_chat_min.file_search_index", "")
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
    redact_pii = bool(
        tools.strtobool(str(_get_icp_param("website_ai_chat_min.redact_pii", False)))
        if hasattr(tools, "strtobool")
        else _get_icp_param("website_ai_chat_min.redact_pii", False)
    )

    # Defaults; can be made ICP-driven later
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
        "file_store_id": file_store_id,
        "file_search_index": file_search_index,
        "allowed_regex": allowed_regex,
        "redact_pii": redact_pii,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }


# -----------------------------------------------------------------------------
# Request parsing (accepts {question} or JSON-RPC body)
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
# Session-based "memory" for chat history
_SESSION_MEM_KEY = "ai_chat_history_v1"


def _mem_bucket_key(cfg: Dict[str, Any]) -> str:
    # isolate memory per provider/model/store
    return (
        f"{(cfg.get('provider') or '').strip()}::"
        f"{(cfg.get('model') or '').strip()}::"
        f"{(cfg.get('file_store_id') or '').strip()}"
    )


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


def _mem_append(
    cfg: Dict[str, Any],
    role: str,
    text: str,
    max_msgs: int = 30,
    max_chars: int = 24000,
) -> None:
    """Append a turn and trim for context window."""
    history = _mem_load(cfg)
    history.append({"role": role, "parts": [{"text": (text or "")[:8000]}]})
    # Trim by message count
    if len(history) > max_msgs:
        history = history[-max_msgs:]
    # Trim by character count (from the end)
    total = 0
    trimmed: List[Dict[str, Any]] = []
    for msg in reversed(history):
        part = (msg.get("parts") or [{}])[0].get("text") or ""
        total += len(part)
        trimmed.append(msg)
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
# Helpers to integrate with aic.admin
def _get_user_gemini_config(env, user) -> Optional[Dict[str, Any]]:
    """Return per-user Gemini config from aic.admin.

    Structure:
        {
            "model_code": "<gemini-model-string>",
            "prompt_limit": int,
            "tokens_per_prompt": int,
        }

    The current implementation simply picks the first active line from the
    user's aic.admin record. If you later introduce a "default" flag or let
    users pick a model on the UI, this is the function to adapt.
    """
    try:
        AicAdmin = env["aic.admin"].sudo()
        if not user:
            return None

        admin_rec = AicAdmin.search(
            [("aic_user_id", "=", user.id), ("active", "=", True)],
            limit=1,
        )
        if not admin_rec:
            return None

        # Take the first active line, if any
        line = admin_rec.aic_line_ids.filtered(lambda l: l.active)[:1]
        if not line:
            return None

        line = line[0]
        model_code = (line.aic_model_id.aic_gemini_model or "").strip()
        if not model_code:
            return None

        return {
            "model_code": model_code,
            "prompt_limit": int(line.aic_prompt_limit or 0),
            "tokens_per_prompt": int(line.aic_tokens_per_prompt or 0),
        }
    except Exception as exc:  # pragma: no cover - safety net
        _logger.error(
            "Failed to fetch Gemini config from aic.admin for user %s: %s",
            getattr(user, "id", "n/a"),
            tools.ustr(exc),
            exc_info=True,
        )
        return None


# -----------------------------------------------------------------------------
# Controller
class AiChatController(http.Controller):
    @http.route("/ai_chat/can_load", type="json", auth="user", csrf=True, methods=["POST"])
    def can_load(self):
        """Determine whether the current user can see/use the AI chat widget.

        New rule (Gemini + aic.admin only):

        - A user can use the chat *only* if there is an active aic.admin
          record for them (no security group check anymore).
        """
        try:
            user = request.env.user
            admin_conf = (
                request.env["aic.admin"]
                .sudo()
                .search([("aic_user_id", "=", user.id), ("active", "=", True)], limit=1)
            )
            return {"show": bool(admin_conf)}
        except Exception as exc:
            _logger.error("AI Chat can_load failed: %s", tools.ustr(exc), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        """Main chat entrypoint: validates input, enforces user limits, calls Gemini."""

        # Basic per-IP throttle
        if not _throttle():
            return {
                "ok": False,
                "reply": _("Please wait a moment before sending another message."),
            }

        # Extract payload
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        cfg = _get_ai_config()
        if not cfg["api_key"]:
            return {
                "ok": False,
                "reply": _(
                    "AI provider API key is not configured. Please contact the administrator."
                ),
            }

        user = request.env.user

        # Per-user Gemini configuration from aic.admin (model + limits)
        user_cfg = _get_user_gemini_config(request.env, user)
        if not user_cfg:
            return {
                "ok": False,
                "reply": _(
                    "You are not allowed to use the AI chat. No active configuration was found."
                ),
            }

        model_code = user_cfg["model_code"]
        prompt_limit = user_cfg["prompt_limit"]
        tokens_per_prompt = user_cfg["tokens_per_prompt"]

        # Override model from aic.admin
        cfg["model"] = model_code

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
            return {
                "ok": False,
                "reply": _("Your question is not within the allowed scope."),
            }

        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # Compose system prompt
        system_text = _build_system_preamble(cfg["system_prompt"], [])

        # If File Search isn't enabled, ensure we don't attach a store
        effective_store = cfg["file_store_id"] if cfg["file_search_enabled"] else ""
        cfg["file_store_id"] = effective_store

        # Enforce tokens-per-prompt cap by shrinking max_tokens in the provider config
        if tokens_per_prompt > 0:
            cfg["max_tokens"] = min(cfg["max_tokens"], tokens_per_prompt)

        # Enforce prompt quota over a 24h window using in-memory buckets
        if not _check_user_quota(user.id, model_code, prompt_limit):
            return {
                "ok": False,
                "reply": _(
                    "You have reached your prompt limit for this Gemini model in the last 24 hours."
                ),
            }

        # Cache lookup — include provider, model, and effective store
        cache_key = f"{cfg['provider']}|{cfg['model']}|{cfg.get('file_store_id', '')}|{outbound_q}"
        cached = _QA_CACHE.get(cache_key)
        if cached:
            answer_md = cached.get("answer_md") or ""
            ui = {
                "title": "",
                "summary": "",
                "answer_md": answer_md,
                "citations": cached.get("citations") or [],
                "suggestions": cached.get("suggestions") or [],
                "ai_status": {
                    "provider": cfg["provider"],
                    "model": cfg["model"],
                    "store": effective_store or None,
                },
                # Expose per-user limits so the frontend can show them (if desired)
                "user_limits": {
                    "prompt_limit": prompt_limit,
                    "tokens_per_prompt": tokens_per_prompt,
                },
            }
            return {"ok": True, "reply": answer_md, "ui": ui}

        # ── MEMORY: append user turn, build contents, call Gemini, append model turn ─────
        provider = _get_provider(cfg)
        try:
            # 1) remember the new user turn
            _mem_append(cfg, "user", outbound_q)

            # 2) compose multi-turn contents (system preamble + history)
            contents = _mem_contents(cfg, system_text)

            # 3) ask with the full contents
            answer_text = provider.ask(system_text, contents).strip()

            # 4) remember the model's reply
            _mem_append(cfg, "model", answer_text)
        except Exception as exc:
            _logger.error(
                "AI Chat Gemini provider call failed: %s",
                tools.ustr(exc),
                exc_info=True,
            )
            return {
                "ok": False,
                "reply": _("Network or provider error. Please try again."),
            }

        # Shape minimal UI (now includes ai_status + user_limits)
        answer_md = answer_text[:1800] if answer_text else ""
        ui = {
            "title": "",
            "summary": "",
            "answer_md": answer_md,
            "citations": [],
            "suggestions": [],
            "ai_status": {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "store": effective_store or None,
            },
            "user_limits": {
                "prompt_limit": prompt_limit,
                "tokens_per_prompt": tokens_per_prompt,
            },
        }

        # Cache answer and non-user-specific metadata
        _QA_CACHE[cache_key] = {
            "answer_md": ui["answer_md"],
            "citations": ui["citations"],
            "suggestions": ui["suggestions"],
        }

        return {
            "ok": True,
            "reply": (ui["answer_md"] or _("(No answer returned.)")),
            "ui": ui,
        }
