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
# docs-only flag) to the AI's reply and the constructed UI payload.
_QA_CACHE: Dict[Tuple[str, bool], Dict[str, object]] = {}

# -----------------------------------------------------------------------------
# Document listing helper
#
# Users sometimes ask to list the available documents.  This helper collects
# filenames from the configured docs folder.  It walks subdirectories and
# returns a sorted list of files with supported extensions.
def _list_documents(root_folder: str) -> List[str]:
    if not root_folder or not os.path.exists(root_folder):
        return []
    doc_list: List[str] = []
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if fn.lower().endswith((".pdf", ".docx", ".xlsx", ".pptx")):
                doc_list.append(fn)
    return sorted(doc_list)

def _is_list_all_documents(q: str) -> bool:
    """Return True if the user asked to list all documents."""
    text = (q or "").lower().strip()
    if re_std.match(r"^(list|show)\b.*\b(documents|docs)\b", text):
        return True
    if re_std.search(r"\b(documents|docs)\b.*\bavailable\b", text):
        return True
    return False

# ---------------- Defaults / Tunables (override via ICP) ----------------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

AI_DEFAULT_TIMEOUT = 15
AI_DEFAULT_TEMPERATURE = 0.2
AI_DEFAULT_MAX_TOKENS = 512

ROUTER_OFFER_T = 0.45

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
    """Simple rate-limiter based on session history."""
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
    """Extract the question string from the request parameters."""
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

# ---------------- PII redaction ----------------
def _redact_pii(text: str) -> str:
    """Mask email addresses, phone numbers and generic IDs in text."""
    try:
        if not text:
            return text
        text = re_std.sub(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"***@***", text)
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)  # rough phones
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)  # simple IDs
        return text
    except Exception:
        return text

# ---------------- Selective RAG router ----------------
DOC_TRIGGERS = [
    r"\bqms\b", r"\bsop(s)?\b", r"\bpolicy\b", r"\bprocedure\b", r"\bwork instruction\b",
    r"\bclause\b", r"\bsection\b", r"\bappendix\b", r"\biso\s*9001\b",
    r"\baccording to (our|the) (doc(ument)?|policy|sop|qms)\b",
    r"\b(pdf|docx|xlsx)\b",
]
DOC_ID_PREFIXES = [
    r"\b[A-Z]{2,5}-[A-Z]{2,5}-(PRC|POL|WI)-\d{3,6}\b",
    r"\bIT-SOP-\d+\b", r"\bHR-POL-\d+\b",
]

def _router_score(q: str) -> float:
    """Compute a simple heuristic score indicating whether to consult docs."""
    text = (q or "").lower()
    score = 0.0
    for pat in DOC_TRIGGERS:
        if re_std.search(pat, text):
            score += 0.22
    for pat in DOC_ID_PREFIXES:
        if re_std.search(pat, text):
            score += 0.35
    if any(x in text for x in ("cite", "citation", "source", "per policy", "per sop", "per qms")):
        score += 0.15
    return min(score, 1.0)

def _router_decide(q: str, force: bool = False) -> tuple[str, float, str]:
    """
    Decide whether to retrieve document snippets or rely on general AI.

    This implementation always returns "retrieve" so that every question
    triggers a scan of the configured documents folder.  The confidence score
    is set based on the router score to preserve logging context.
    """
    if force:
        return "retrieve", 1.0, "forced"
    # Always retrieve for simplicity; more advanced routing could be enabled
    s = _router_score(q)
    return "retrieve", s, f"forced_retrieve score={s:.2f}"

# ---------------- PDF retrieval stub ----------------
# def _read_pdf_snippets(root_folder: str, query: str) -> List[Tuple[str, int, str]]:
#     """
#     Deprecated PDF scanning helper.
#
#     Local PDF scanning and snippet extraction has been removed in favor of
#     Gemini File Search.  This stub remains to maintain compatibility with
#     existing code paths.  It always returns an empty list.
#     """
#     return []

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
        """Retry wrapper with simple backoff."""
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
        """Call the OpenAI API (chat completion)."""
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
            )
            return (resp.choices[0].message.content or "").strip()
        return self._with_retries(_call)

class _GeminiProvider(_ProviderBase):
    def generate(self, prompt: str, user_text: str) -> str:
        """Generate a response from Gemini using plain text."""
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
            r = model.generate_content(
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=prompt,
                request_options={"timeout": self.timeout},
            )
        except Exception:
            # Fallback if system_instruction is unsupported
            r = model.generate_content(
                [{"role": "user", "parts": [{"text": f"{prompt}\n\n{user_text}"}]}],
                request_options={"timeout": self.timeout},
            )
        return (getattr(r, "text", None) or "").strip()

    def generate_with_file_search(self, prompt: str, user_text: str, store_name: str) -> str:
        """Generate a response using Gemini's File Search tool."""
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
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
            # Fallback: merge prompt and question while preserving config
            try:
                r = model.generate_content(
                    [{"role": "user", "parts": [{"text": f"{prompt}\n\n{user_text}"}]}],
                    request_options={"timeout": self.timeout},
                    config=gen_config,
                )
            except Exception:
                # Final fallback: call without File Search
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
def _build_system_preamble(system_prompt: str, snippets: List[Tuple[str, int, str]], only_docs: bool) -> str:
    """Compose the system preamble for the LLM call."""
    lines = []
    base = (system_prompt or "").strip()
    if base:
        lines.append(base)
    if only_docs:
        lines.append(
            "You MUST answer **only** using the provided excerpts. If they do not contain the answer, reply exactly: \"I don’t know based on the current documents.\" Do not add outside knowledge."
        )
    else:
        lines.append("Prefer the provided excerpts; be concise if you rely on general knowledge.")
    lines.append(
        "Formatting: Keep it compact. No more than 10 bullets or 200 words. "
        "Always include a short 'summary'."
    )
    if snippets:
        lines.append("Relevant excerpts:")
        for fn, page, text in snippets:
            lines.append(f"[{fn}] {text}")
    return "\n".join(lines)

# ---------------- Config loader ----------------
def _get_ai_config():
    """Load chat configuration from system parameters."""
    provider = _get_icp_param("website_ai_chat_min.ai_provider", "openai")
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    model = _get_icp_param("website_ai_chat_min.ai_model", "")
    system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "")
    allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
    docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "")
    only_docs = _bool_icp("website_ai_chat_min.answer_only_from_docs", False)
    timeout = _int_icp("website_ai_chat_min.ai_timeout", AI_DEFAULT_TIMEOUT)
    temperature = _float_icp("website_ai_chat_min.ai_temperature", AI_DEFAULT_TEMPERATURE)
    max_tokens = _int_icp("website_ai_chat_min.ai_max_tokens", AI_DEFAULT_MAX_TOKENS)
    redact_pii = _bool_icp("website_ai_chat_min.redact_pii", False)
    file_search_store = _get_icp_param("website_ai_chat_min.file_search_store", "")
    file_search_enabled = _bool_icp("website_ai_chat_min.file_search_enabled", False)

    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "system_prompt": system_prompt,
        "allowed_regex": allowed_regex,
        "docs_folder": docs_folder,
        "only_docs": only_docs,
        "timeout": timeout,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "redact_pii": redact_pii,
        "file_search_store": file_search_store,
        "file_search_enabled": file_search_enabled,
    }

# ---------------- Markdown fence/JSON extraction helpers ----------------
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
    if s.startswith("```"):
        s = _FENCE_OPEN.sub("", s, count=1)
        s = _FENCE_CLOSE.sub("", s, count=1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
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
    """Main entry point for the AI chat widget."""

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
    def send(self, question=None, force: bool = False):
        """
        Validate and route the question, call the AI provider, and return a structured reply.
        """
        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        try:
            raw = request.httprequest.get_data(cache=False, as_text=True)
            if raw:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    force = bool(payload.get("force", force))
        except Exception:
            pass

        cfg = _get_ai_config()
        if not cfg["api_key"]:
            return {"ok": False, "reply": _("AI provider API key is not configured. Please contact the administrator.")}
        if cfg["allowed_regex"] and not _match_allowed(cfg["allowed_regex"], q):
            return {"ok": False, "reply": _("Your question is not within the allowed scope.")}
        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # Determine whether to use Gemini File Search (enabled & store specified)
        use_file_search = bool(
            cfg.get("file_search_enabled")
            and cfg.get("provider") == "gemini"
            and cfg.get("file_search_store")
        )

        # Special handling: list all documents
        if _is_list_all_documents(q):
            list_key = ("__list_docs__", cfg["only_docs"])
            cached_list = _QA_CACHE.get(list_key)
            if cached_list:
                return {"ok": True, "reply": cached_list["reply"], "ui": dict(cached_list["ui"])}
            docs = _list_documents(cfg["docs_folder"])
            if not docs:
                answer_list = _("No documents were found in the configured folder. Please check your settings.")
            else:
                MAX_LIST = 50
                truncated = docs[:MAX_LIST]
                bullets = "\n".join(f"• {fn}" for fn in truncated)
                suffix = "" if len(docs) <= MAX_LIST else _(
                    f"\n\n(Showing {MAX_LIST} of {len(docs)} documents)"
                )
                answer_list = f"{bullets}{suffix}"
            ui = {
                "title": "Document list",
                "summary": _(f"{len(docs)} document{'s' if len(docs) != 1 else ''} found.") if docs else "",
                "answer_md": answer_list,
                "citations": [],
                "suggestions": [],
            }
            _QA_CACHE[list_key] = {"reply": ui["answer_md"], "ui": dict(ui)}
            return {"ok": True, "reply": ui["answer_md"], "ui": ui}

        # Caching: check for prior identical queries
        cache_key = (outbound_q, cfg["only_docs"])
        cached = _QA_CACHE.get(cache_key)
        if cached:
            return {"ok": True, "reply": cached["reply"], "ui": dict(cached["ui"])}

        request_only_docs = cfg["only_docs"]

        # Routing & retrieval
        route_action, confidence, route_reason = _router_decide(q, force=force)
        doc_snippets: List[Tuple[str, int, str]] = []
        t_scan0 = time.time()

        scan_ms = int((time.time() - t_scan0) * 1000)

        # Docs-only immediate response if no snippets
        if request_only_docs and not doc_snippets:
            ui = {
                "title": "",
                "summary": "",
                "answer_md": _("Please provide a document number or specific topic for a more detailed response."),
                "citations": [],
                "suggestions": [],
            }
            _QA_CACHE[cache_key] = {"reply": ui["answer_md"], "ui": dict(ui)}
            return {"ok": True, "reply": ui["answer_md"], "ui": ui}

        # Compose prompts
        system_text = _build_system_preamble(cfg["system_prompt"], doc_snippets, request_only_docs)
        if route_action == "answer_with_offer":
            outbound_q += "\n\n(If helpful, I can check internal documents for the exact clause.)"

        # Call provider
        provider = _get_provider(cfg)
        t_ai0 = time.time()
        try:
            reply = provider.generate(system_text, outbound_q)
        except Exception as e:
            _logger.error("[AIChat] Provider error (%s/%s): %s",
                          cfg["provider"], cfg["model"] or "<default>", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("The AI service is temporarily unavailable. Please try again shortly.")}
        finally:
            ai_ms = int((time.time() - t_ai0) * 1000)
            _logger.info(
                "[AIChat] route=%s(%s) conf=%.2f provider=%s model=%s scan_ms=%s ai_ms=%s snippets=%s pii=%s",
                route_action, route_reason, confidence, cfg["provider"], cfg["model"] or "<default>",
                scan_ms, ai_ms, len(doc_snippets), bool(cfg["redact_pii"])
            )

        # Post-process provider reply
        answer_text: str = (reply or "").strip()
        citations: List[dict] = []
        try:
            parsed = extract_json_obj(reply or "")
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            try:
                answer_text = str(
                    parsed.get("answer_md") or parsed.get("text") or parsed.get("reply") or answer_text
                )
            except Exception:
                pass
        answer_text = _strip_md_fences(answer_text.strip())
        try:
            answer_text = re_std.sub(
                r"Please include the document number/code.*?to narrow the result\.\s*", "",
                answer_text,
                flags=re_std.I,
            )
        except Exception:
            pass
        try:
            idx = answer_text.lower().find('"citations"')
            if idx >= 0:
                answer_text = answer_text[:idx].rstrip()
        except Exception:
            pass
        try:
            answer_text = re_std.sub(r"\[[^\]]*? p\.\d+(?:,\s*p\.\d+)*\]", "", answer_text)
        except Exception:
            pass

        # Add prefix based on retrieval results (skip prefix when using File Search)
        prefix = ""
        try:
            if not use_file_search and (request_only_docs or route_action == "retrieve"):
                if not doc_snippets:
                    prefix = _(
                        "I can't find any references from our internal documents. Here’s what I found: "
                    )
                else:
                    titles = []
                    seen_titles = set()
                    for fn, _page, _snippet in doc_snippets:
                        base = os.path.splitext(fn)[0]
                        if base not in seen_titles:
                            seen_titles.add(base)
                            titles.append(base)
                    title_str = ", ".join(f"‘{t}’" for t in titles[:5])
                    prefix = _(f"According to these documents {title_str}: ")
        except Exception:
            prefix = ""
        answer_text = prefix + answer_text

        # Build UI payload
        ui = {
            "title": "",
            "summary": "",
            "answer_md": answer_text,
            "citations": [],
            "suggestions": [],
        }
        MAX_ANSWER_CHARS = 1800
        ui["answer_md"] = (ui["answer_md"] or "")[:MAX_ANSWER_CHARS]
        _QA_CACHE[cache_key] = {"reply": ui["answer_md"], "ui": dict(ui)}
        return {"ok": True, "reply": (ui["answer_md"] or _("(No answer returned.)")), "ui": ui}
