# -*- coding: utf-8 -*-
from __future__ import annotations

from odoo import http, tools, _
from odoo.http import request
from odoo.exceptions import AccessDenied

import json
import os
import time
import re as re_std
import logging
from typing import Dict, List, Tuple

import json, re

_logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Caching layer
#
# To speed up repeated lookups, we maintain a simple in-memory cache of
# previously asked questions.  Each entry maps a tuple of (question text,
#  flag) to the AI's reply and the constructed UI payload.  This
# reduces latency when users ask the same question multiple times and avoids
# redundant document scans and provider calls.  Note: this cache lives in
# process memory; it resets when the Odoo server restarts.  Administrators
# can replace this with a more robust cache (e.g., Redis) if needed.
_QA_CACHE: Dict[str, Dict[str, object]] = {}

# -----------------------------------------------------------------------------
# function covers commands like "list all documents" and natural
# questions like "what are the documents available?".

# ---------------- Defaults / Tunables (override via ICP) ----------------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

AI_DEFAULT_TIMEOUT = 15
AI_DEFAULT_TEMPERATURE = 0.2
AI_DEFAULT_MAX_TOKENS = 512

_FENCE_OPEN = re.compile(r'^\s*```[a-zA-Z0-9_-]*\s*')
_FENCE_CLOSE = re.compile(r'\s*```\s*$')

# ---------------- Optional libs ----------------
try:
    import regex as regex_safe  # type: ignore
except Exception:
    regex_safe = None

# ---------------- ICP helpers ----------------
def _icp():
    return request.env["ir.config_parameter"].sudo()

def _get_icp_param(name, default=""):
    try:
        val = _icp().get_param(name, default)
        return val if val not in (None, "") else default
    except Exception:
        return default

def _bool_icp(key: str, default: bool = False) -> bool:
    return tools.str2bool(_get_icp_param(key, "1" if default else "0"))

def _int_icp(key: str, default: int) -> int:
    try:
        return int(_get_icp_param(key, default))
    except Exception:
        return default

def _float_icp(key: str, default: float) -> float:
    try:
        return float(_get_icp_param(key, default))
    except Exception:
        return default

# ---------------- Auth / visibility ----------------
def _is_logged_in(env) -> bool:
    try:
        return bool(env.user and not env.user._is_public())
    except Exception:
        return False

def _can_show_widget(env) -> bool:
    # Bubble visible to logged-in users only
    return _is_logged_in(env)

# ---------------- Rate limiting ----------------
def _client_ip():
    try:
        xfwd = request.httprequest.headers.get("X-Forwarded-For", "")
        ip = (xfwd.split(",")[0].strip() if xfwd else request.httprequest.remote_addr) or "0.0.0.0"
        return ip
    except Exception:
        return "0.0.0.0"

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
    except Exception:
        return True

# ---------------- Query normalization / scope ----------------
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

def _match_allowed(pattern: str, text: str, timeout_ms=100) -> bool:
    """Allow-list regex with timeout to resist ReDoS. Fail-closed if 'regex' is unavailable."""
    if not pattern:
        return True
    try:
        if regex_safe:
            return bool(regex_safe.search(pattern, text, flags=regex_safe.I | regex_safe.M, timeout=timeout_ms))
        # Fail-closed if admin configured a pattern but 'regex' is not available
        return False
    except Exception:
        return False

# ---------------- PII redaction ----------------
def _redact_pii(text: str) -> str:
    try:
        if not text:
            return text
        text = re_std.sub(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"***@***", text)
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)  # rough phones
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)  # simple IDs
        return text
    except Exception:
        return text

# ---------------- Provider adapters ----------------

class _ProviderBase:
    def __init__(self, api_key: str, model: str, timeout: int, temperature: float, max_tokens: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
    def generate(self, system_text: str, user_text: str) -> str:
        raise NotImplementedError
    def _with_retries(self, fn):
        delays = [0.5, 1.0]
        last_exc = None
        for i in range(len(delays) + 1):
            try:
                return fn()
            except Exception as e:
                last_exc = e
                if i < len(delays):
                    time.sleep(delays[i])
        if last_exc:
            raise last_exc
        return ""

class _OpenAIProvider(_ProviderBase):
    def generate(self, system_text: str, user_text: str) -> str:
        """
        Call the OpenAI chat completion API.  We do **not** specify a
        `response_format` so the model is free to return plain text rather than
        structured JSON.  Adding a JSON response format can cause the model
        to announce itself as a JSON generator, which confused users.  Any
        structured data (e.g., JSON fences) returned by the model is
        post-processed downstream if present.
        """
        def _call():
            from openai import OpenAI  # type: ignore
            client = OpenAI(api_key=self.api_key, timeout=self.timeout)
            messages = []
            if system_text:
                messages.append({"role": "system", "content": system_text})
            messages.append({"role": "user", "content": user_text})
            resp = client.chat.completions.create(
                model=(self.model or OPENAI_DEFAULT_MODEL),
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                # Do not force JSON responses; let the model produce natural language
            )
            return (resp.choices[0].message.content or "").strip()
        return self._with_retries(_call)

class _GeminiProvider(_ProviderBase):
    def generate(self, prompt: str, user_text: str) -> str:
        """
        Generate a response from Gemini using plain text.

        This method configures the generative model with the current
        temperature and token limits and avoids forcing JSON output.  A
        system instruction is supplied separately from the user text when
        supported by the SDK; otherwise the two are concatenated in a
        fallback call.

        :param prompt: System instructions to guide the assistant.
        :param user_text: The user's question (already redacted if
            necessary).
        :returns: The model's reply as a plain string.
        """
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
        model_name = self.model or "gemini-2.5-flash"
        model = genai.GenerativeModel(
            model_name,
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            },
        )
        try:
            # Preferred call signature: separate system instruction.
            r = model.generate_content(
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=prompt,
                request_options={"timeout": self.timeout},
            )
        except Exception:
            # Fallback for SDKs that don't support system_instruction.
            r = model.generate_content(
                [{"role": "user", "parts": [{"text": f"{prompt}\n\n{user_text}"}]}],
                request_options={"timeout": self.timeout},
            )
        return (getattr(r, "text", None) or "").strip()

    def generate_with_file_search(self, prompt: str, user_text: str, store_name: str) -> str:
        """
        Generate a response using Gemini's File Search tool.

        When a FileSearchStore name is provided, this method constructs a
        File Search tool configuration and passes it to the generative model.
        The model retrieves relevant document snippets from the store and
        incorporates them into its response.  Multiple fallbacks ensure
        graceful degradation if the SDK version or arguments differ.

        :param prompt: System preamble instructions.
        :param user_text: The user's question.
        :param store_name: Name of the FileSearchStore (e.g.
            "fileSearchStores/my-file-store").
        :returns: The assistant's reply as a string.
        """
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
        # Attempt to build typed tool configurations; fall back to dicts if
        # imports fail or types are unavailable.
        try:
            from google.genai import types  # type: ignore
            fs_tool = types.Tool(
                file_search=types.FileSearch(
                    file_search_store_names=[store_name]
                )
            )
            gen_config = types.GenerateContentConfig(tools=[fs_tool])
        except Exception:
            fs_tool = {"file_search": {"file_search_store_names": [store_name]}}
            gen_config = {"tools": [fs_tool]}

        model_name = self.model or "gemini-2.5-flash"
        model = genai.GenerativeModel(
            model_name,
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            },
        )

        try:
            r = model.generate_content(
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=prompt,
                request_options={"timeout": self.timeout},
                config=gen_config,
            )
        except Exception:
            # Fallback: merge prompt and question while preserving config.
            try:
                r = model.generate_content(
                    [{"role": "user", "parts": [{"text": f"{prompt}\n\n{user_text}"}]}],
                    request_options={"timeout": self.timeout},
                    config=gen_config,
                )
            except Exception:
                # Final fallback: call without File Search.  This may return a
                # plausible answer but will not include retrieved context.
                r = model.generate_content(
                    [{"role": "user", "parts": [{"text": user_text}]}],
                    request_options={"timeout": self.timeout},
                )
        return (getattr(r, "text", None) or "").strip()

def _get_provider(cfg: dict) -> _ProviderBase:
    provider = (cfg.get("provider") or "openai").strip().lower()
    if provider == "gemini":
        return _GeminiProvider(cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"])
    return _OpenAIProvider(cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"])

# ---------------- Prompt composition ----------------
def _build_system_preamble(system_prompt: str, snippets: List[Tuple[str, int, str]]) -> str:
    lines = []
    base = (system_prompt or "").strip()
    if base:
        lines.append(base)
    else:
        lines.append("Prefer the provided excerpts; be concise if you rely on general knowledge.")

    lines.append(
        "Formatting: Keep it compact. No more than 10 bullets or 200 words. "
        "Always include a short 'summary'."
    )

    if snippets:
        # Present relevant excerpts without page numbers.  Removing page numbers
        # prevents the assistant from citing page positions in the answer.
        lines.append("Relevant excerpts:")
        for fn, page, text in snippets:
            lines.append(f"[{fn}] {text}")
    return "\n".join(lines)

# ---------------- Config loader ----------------
def _get_ai_config():
    provider = _get_icp_param("website_ai_chat_min.ai_provider", "openai")
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    model = _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "")
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
    timeout = _int_icp("website_ai_chat_min.ai_timeout", AI_DEFAULT_TIMEOUT)
    temperature = _float_icp("website_ai_chat_min.ai_temperature", AI_DEFAULT_TEMPERATURE)
    max_tokens = _int_icp("website_ai_chat_min.ai_max_tokens", AI_DEFAULT_MAX_TOKENS)
    redact_pii = _bool_icp("website_ai_chat_min.redact_pii", False)

    # Load additional configuration for optional Gemini File Search integration.
    file_search_store = _get_icp_param("website_ai_chat_min.file_search_store", "")
    file_search_enabled = _bool_icp("website_ai_chat_min.file_search_enabled", False)

    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "system_prompt": system_prompt,
        "allowed_regex": allowed_regex,
        "timeout": timeout,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "redact_pii": redact_pii,
        # New fields: file search configuration
        "file_search_store": file_search_store,
        "file_search_enabled": file_search_enabled,
    }

# ---------------- Markdown fence/JSON extraction helpers (NEW) ----------------
def _strip_md_fences(s: str) -> str:
    """Remove ```...``` fences (with optional language tag) from a single block."""
    try:
        if not s:
            return s
        t = s.strip()
        m = re_std.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*)\n```$", t, re_std.S)
        return (m.group(1).strip() if m else t)
    except Exception:
        return s

def extract_json_obj(s: str):
    """Return a dict parsed from the first balanced JSON object inside s; else None."""
    if not s:
        return None
    s = s.strip()

    # strip leading/trailing code fences if present
    if s.startswith("```"):
        s = _FENCE_OPEN.sub("", s, count=1)
        s = _FENCE_CLOSE.sub("", s, count=1).strip()

    # fast path: exact JSON
    try:
        return json.loads(s)
    except Exception:
        pass

    # scan for first balanced {...}
    start = s.find("{")
    if start == -1:
        return None

    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i+1])
                    except Exception:
                        break
    return None

# ---------------- HTTP Controller ----------------
class WebsiteAIChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=True, methods=["POST"])
    def can_load(self):
        """Login gate for mounting the widget; returns a minimal boolean."""
        try:
            show = _can_show_widget(request.env)
            return {"show": bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        """
        Validates, composes prompt,
        calls provider with retries, and returns a compact, structured reply.
        """
        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        # Extract payload (accepts {question} or JSON-RPC envelope)
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}
        cfg = _get_ai_config()
        if not cfg["api_key"]:
            return {"ok": False, "reply": _("AI provider API key is not configured. Please contact the administrator.")}

        if cfg["allowed_regex"] and not _match_allowed(cfg["allowed_regex"], q):
            return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # Determine whether the request should use Gemini File Search. This flag is
        # True only when the Gemini provider is selected, File Search is explicitly
        # enabled, and a store name is configured. Without this, the legacy PDF
        # scanner (now stubbed) will be used.
        use_file_search = bool(
            cfg.get("file_search_enabled")
            and cfg.get("provider") == "gemini"
            and cfg.get("file_search_store")
        )

        # ------------------------------------------------------------------
        # Special handling: list all documents
        #
        # If the user explicitly asks to list available documents, return a
        # bullet list of filenames from the docs folder.  This bypasses the
        # AI provider entirely and caches the result under a dedicated key.
