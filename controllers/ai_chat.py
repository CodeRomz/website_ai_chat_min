# -*- coding: utf-8 -*-
"""
Production-hardened AI chat controller:
- Login-only visibility by default; optional group-gated visibility when configured.
- Clear JSON responses (no AccessDenied exceptions bubbling to the client).
- CSRF protected /send, robust CSRF headers on the client.
- Rate limiting per (uid, ip) with safe fallback (deny on error).
- Optional allow-list regex with timeout (seconds) and compiled-cache.
- Optional PDF snippets retrieval (bounded) with basic memoization per file page.
- Provider adapters (OpenAI / Gemini) with retries and JSON-contract parsing.

Environment via ICP (ir.config_parameter) with keys under `website_ai_chat_min.*`
"""

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
from typing import Dict, Any, List, Tuple, Optional

# Try faster/safer regex with timeout; fallback to stdlib
try:
    import regex as regex_safe  # type: ignore
except Exception:  # pragma: no cover
    regex_safe = None

import re as re_std

# -----------------------------
# Tunables (override with ICP)
# -----------------------------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15  # seconds

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

AI_DEFAULT_TEMPERATURE = 0.2
AI_DEFAULT_TIMEOUT = 20  # seconds
AI_DEFAULT_MAX_TOKENS = 512

DOC_MAX_FILES = 30
DOC_MAX_PAGES_PER_FILE = 16
DOC_MAX_HITS = 12

# -----------------------------
# Helpers: ICP and coercers
# -----------------------------
def _icp():
    return request.env["ir.config_parameter"].sudo()

def _get_icp_param(name: str, default: str = "") -> str:
    try:
        return _icp().get_param(name, default) or default
    except Exception as e:
        _logger.warning("ICP read failed for %s: %s", name, tools.ustr(e))
        return default

def _bool_icp(name: str, default: bool = False) -> bool:
    val = _get_icp_param(name, "")
    return default if val == "" else val.strip().lower() in ("1", "true", "yes", "on")

def _int_icp(name: str, default: int) -> int:
    try:
        return int(_get_icp_param(name, str(default)))
    except Exception:
        return default

def _float_icp(name: str, default: float) -> float:
    try:
        return float(_get_icp_param(name, str(default)))
    except Exception:
        return default

# -----------------------------
# Authz & visibility
# -----------------------------
def _is_logged_in(env) -> bool:
    try:
        return not (env.user._is_public())
    except Exception:
        return False

def _user_has_required_group(env) -> bool:
    """If require_group_xmlid is configured, user must be in that group."""
    xmlid = _get_icp_param("website_ai_chat_min.require_group_xmlid", "")
    if not xmlid:
        return True
    try:
        grp = env.ref(xmlid)
    except Exception:
        _logger.error("Configured group xmlid %s not found; denying.", xmlid)
        return False
    try:
        return grp in env.user.groups_id
    except Exception:
        return False

def _can_show_widget(env) -> bool:
    """By default: show to logged-in users.
       If group xmlid is configured, require that group for visibility as well."""
    if not _is_logged_in(env):
        return False
    if _get_icp_param("website_ai_chat_min.require_group_xmlid", ""):
        return _user_has_required_group(env)
    return True

# -----------------------------
# Abuse controls
# -----------------------------
def _client_ip() -> str:
    # Honor reverse proxy headers (ensure Nginx sets X-Forwarded-For)
    try:
        xff = request.httprequest.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return request.httprequest.remote_addr or "0.0.0.0"
    except Exception:
        return "0.0.0.0"

def _get_rate_limits() -> Tuple[int, int]:
    max_req = _int_icp("website_ai_chat_min.rate_limit_max", DEFAULT_RATE_LIMIT_MAX)
    window = _int_icp("website_ai_chat_min.rate_limit_window", DEFAULT_RATE_LIMIT_WINDOW)
    return max(1, max_req), max(1, window)

def _throttle() -> bool:
    """Return True if allowed, False if rate-limited or error."""
    try:
        max_req, window = _get_rate_limits()
        now = int(time.time())
        uid = request.env.user.id or 0
        ip = _client_ip()
        key = f"ai_chat_rl::{uid}::{ip}"
        sess = request.session
        stamps = sess.get(key, [])
        stamps = [t for t in stamps if (now - t) < window]
        if len(stamps) >= max_req:
            sess[key] = stamps  # persist cleanup
            return False
        stamps.append(now)
        sess[key] = stamps
        return True
    except Exception as e:
        # Safer default: deny on internal error to avoid abuse
        _logger.error("Throttle error: %s", tools.ustr(e), exc_info=True)
        return False

# -----------------------------
# Input normalization / allow-list
# -----------------------------
def _normalize_message_from_request() -> str:
    """Accepts {question} or JSON-RPC {params:{question}}."""
    try:
        data = request.jsonrequest or {}
        if "question" in data:
            return (data.get("question") or "").strip()
        if "params" in data and isinstance(data["params"], dict):
            return (data["params"].get("question") or "").strip()
    except Exception:
        pass
    return ""

_ALLOWED_CACHE: Dict[str, Any] = {}

def _match_allowed(pattern: str, text: str, timeout_s: float = 0.06) -> bool:
    """Return True if allowed (either no pattern or it matches).
       regex_safe.timeout expects seconds. Cache compiled patterns."""
    if not pattern:
        return True
    try:
        if regex_safe:
            compiled = _ALLOWED_CACHE.get(pattern)
            if not compiled:
                compiled = regex_safe.compile(pattern, flags=regex_safe.I | regex_safe.M)
                _ALLOWED_CACHE[pattern] = compiled
            return bool(compiled.search(text, timeout=timeout_s))
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception as e:
        _logger.warning("allowed_regex error: %s", tools.ustr(e))
        return False

# -----------------------------
# Optional PII redaction (outbound prompt)
# -----------------------------
def _redact_pii(text: str) -> Tuple[str, int]:
    if not text:
        return text, 0
    n = 0
    def subn(pat, repl, s, flags=0):
        nonlocal n
        s2, c = re_std.subn(pat, repl, s, flags=flags)
        n += c
        return s2
    red = text
    red = subn(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", red)
    red = subn(r"(?<!\d)(\+?\d[\d \-]{7,}\d)", "[phone]", red)
    red = subn(r"\b([A-Z0-9]{8,})\b", "[id]", red)
    return red, n

# -----------------------------
# PDF retrieval (bounded) with tiny memo for page text
# -----------------------------
_PDF_PAGE_MEMO: Dict[Tuple[str, int], str] = {}
_PDF_MEMO_MAX = 1024  # simple cap

def _memo_put_pdf_page(key: Tuple[str, int], val: str):
    if len(_PDF_PAGE_MEMO) >= _PDF_MEMO_MAX:
        # pop an arbitrary (FIFO-like) item to keep memory bounded
        _PDF_PAGE_MEMO.pop(next(iter(_PDF_PAGE_MEMO)))
    _PDF_PAGE_MEMO[key] = val

def _extract_pdf_page_text(filepath: str, page_idx: int) -> str:
    """Extract text for (file, page_idx). Uses a basic memo."""
    memo_key = (filepath, page_idx)
    if memo_key in _PDF_PAGE_MEMO:
        return _PDF_PAGE_MEMO[memo_key]
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            _logger.warning("pypdf not installed; cannot scan PDFs")
            return ""
        reader = PdfReader(filepath)
        if page_idx < 0 or page_idx >= len(reader.pages):
            return ""
        txt = reader.pages[page_idx].extract_text() or ""
        _memo_put_pdf_page(memo_key, txt)
        return txt
    except Exception as e:
        _logger.warning("PDF read failed %s p%d: %s", filepath, page_idx, tools.ustr(e))
        return ""

def _read_pdf_snippets(root_folder: str, query: str) -> List[Dict[str, Any]]:
    """Linear, bounded search for pages containing the query (case-insensitive)."""
    results: List[Dict[str, Any]] = []
    if not root_folder or not query:
        return results
    q = query.strip()
    if not os.path.isdir(root_folder):
        _logger.info("docs_folder not a dir: %s", root_folder)
        return results
    # Walk files (bounded)
    n_files = 0
    for root, _dirs, files in os.walk(root_folder):
        for fn in files:
            if not fn.lower().endswith(".pdf"):
                continue
            filepath = os.path.join(root, fn)
            n_files += 1
            if n_files > DOC_MAX_FILES:
                return results
            # Per file, scan first pages (bounded)
            try:
                try:
                    from pypdf import PdfReader  # type: ignore
                except Exception:
                    _logger.warning("pypdf not installed; cannot scan PDFs")
                    return results
                reader = PdfReader(filepath)
                page_count = min(len(reader.pages), DOC_MAX_PAGES_PER_FILE)
                for page_idx in range(page_count):
                    if len(results) >= DOC_MAX_HITS:
                        return results
                    text = _extract_pdf_page_text(filepath, page_idx)
                    if not text:
                        continue
                    if q.lower() in text.lower():
                        # capture small snippet (first occurrence line-ish)
                        snippet = text.strip().splitlines()
                        if snippet:
                            snippet = snippet[0][:320]
                        else:
                            snippet = ""
                        results.append({
                            "file": fn,
                            "page": page_idx + 1,  # 1-based
                            "snippet": snippet,
                        })
            except Exception as e:
                _logger.debug("PDF scan error on %s: %s", filepath, tools.ustr(e))
    return results

# -----------------------------
# Provider adapters (with retries)
# -----------------------------
def _call_openai(system_text: str, user_text: str, model: str, temperature: float, timeout_s: int, max_tokens: int) -> str:
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        _logger.error("openai python client not installed")
        raise
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    if not api_key:
        raise UserError(_("OpenAI API key is missing"))
    client = OpenAI(api_key=api_key)
    last_err = None
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model=model or OPENAI_DEFAULT_MODEL,
                temperature=temperature,
                timeout=timeout_s,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_text},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise last_err or Exception("OpenAI call failed")

def _call_gemini(system_text: str, user_text: str, model: str, temperature: float, timeout_s: int, max_tokens: int) -> str:
    try:
        import google.generativeai as genai  # type: ignore
    except Exception:
        _logger.error("google.generativeai not installed")
        raise
    api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
    if not api_key:
        raise UserError(_("Gemini API key is missing"))
    genai.configure(api_key=api_key)
    last_err = None
    for _ in range(3):
        try:
            gen_model = genai.GenerativeModel(model or GEMINI_DEFAULT_MODEL)
            prompt = f"{system_text}\n\nUser:\n{user_text}"
            resp = gen_model.generate_content(prompt, generation_config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                # Note: official timeouts may be enforced by HTTP layer
            })
            # Gemini may put JSON in the text field
            txt = (resp.text or "").strip()
            return txt
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise last_err or Exception("Gemini call failed")

# -----------------------------
# System prompt / output contract
# -----------------------------
def _build_system_preamble(only_docs: bool, snippets: List[Dict[str, Any]]) -> str:
    base = _get_icp_param("website_ai_chat_min.system_prompt",
                          "You are a precise assistant. Always return compact JSON.")
    pieces = [base.strip()]
    if only_docs:
        pieces.append("You must answer only using the provided document excerpts. "
                      "If not possible, return an empty answer.")
    pieces.append("Output JSON object with keys: title (str), summary (str), "
                  "answer_md (str), citations (list of {file,page}), suggestions (list of str).")
    if snippets:
        pieces.append("Document excerpts:")
        for s in snippets:
            pieces.append(f"[{s.get('file','doc')} p.{s.get('page','?')}] {s.get('snippet','')}")
    return "\n".join(pieces)

def _strip_json_fences(s: str) -> str:
    if not s:
        return s
    st = s.strip()
    if st.startswith("```"):
        st = st.strip("` \n\r\t")
        # remove possible "json" first line
        if "\n" in st:
            first, rest = st.split("\n", 1)
            if first.strip().lower() in ("json", "javascript"):
                return rest.strip()
        return st
    return st

# -----------------------------
# HTTP Controller
# -----------------------------
class WebsiteAIChatMinController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=False, methods=["POST"])
    def can_load(self, **_kwargs):
        """Probe for whether the widget should show for this session."""
        try:
            show = _can_show_widget(request.env)
            return {"show": bool(show)}
        except Exception as e:
            _logger.warning("can_load error: %s", tools.ustr(e))
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, **_kwargs):
        """Main chat endpoint. Always returns a JSON (no exception to client)."""
        t0 = time.time()
        # Visibility/permission check (server-side gate)
        if not _user_has_required_group(request.env):
            return {"ok": False, "reply": _("You are not allowed to use this assistant.")}

        # Rate limit
        if not _throttle():
            return {"ok": False, "reply": _("You are sending messages too quickly. Please wait a moment."), "code": "E-RL"}

        # Normalize input
        question = _normalize_message_from_request()
        if not question:
            return {"ok": False, "reply": _("Please type your question."), "code": "E-EMPTY"}

        # Load config
        provider = (_get_icp_param("website_ai_chat_min.ai_provider", "openai") or "openai").lower()
        api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "")
        model = _get_icp_param("website_ai_chat_min.ai_model",
                               OPENAI_DEFAULT_MODEL if provider == "openai" else GEMINI_DEFAULT_MODEL)
        allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "")
        docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "").strip()
        only_docs = _bool_icp("website_ai_chat_min.answer_only_from_docs", False)
        redact_pii = _bool_icp("website_ai_chat_min.redact_pii", False)

        ai_temperature = _float_icp("website_ai_chat_min.ai_temperature", AI_DEFAULT_TEMPERATURE)
        ai_timeout_s = _int_icp("website_ai_chat_min.ai_timeout", AI_DEFAULT_TIMEOUT)
        ai_max_tokens = _int_icp("website_ai_chat_min.ai_max_tokens", AI_DEFAULT_MAX_TOKENS)

        if not api_key:
            return {"ok": False, "reply": _("Assistant is not configured. (Missing API key)"), "code": "E-CONFIG"}

        # Allow-list scope check
        if allowed_regex and not _match_allowed(allowed_regex, question, timeout_s=0.06):
            return {
                "ok": False,
                "reply": _("Your question doesn't match the allowed scope."),
                "code": "E-SCOPE",
            }

        # Optional PII redaction (outbound prompt only)
        outbound_q = question
        redactions = 0
        if redact_pii:
            outbound_q, redactions = _redact_pii(question)

        # Optional retrieval from PDFs (bounded)
        snippets: List[Dict[str, Any]] = []
        if docs_folder:
            t_pdf = time.time()
            snippets = _read_pdf_snippets(docs_folder, outbound_q)
            _logger.info("PDF scan hits=%s ms=%d", len(snippets), int((time.time() - t_pdf) * 1000))

        # Strict “only from docs” guard: if required but no snippets → guided reply
        if only_docs and not snippets:
            ui = {
                "title": _("No matching documents found"),
                "summary": _("I can only answer from the configured documents."),
                "answer_md": _("Please include the document number/code or a more specific keyword."),
                "citations": [],
                "suggestions": [
                    _("Search by document number (e.g., INV-2024-00123)"),
                    _("Search by product code (e.g., ABC-123)"),
                ],
            }
            return {"ok": True, "reply": ui["summary"], "ui": ui}

        # Build system + call provider
        system_text = _build_system_preamble(only_docs, snippets)
        try:
            if provider == "openai":
                raw = _call_openai(system_text, outbound_q, model, ai_temperature, ai_timeout_s, ai_max_tokens)
            elif provider == "gemini":
                raw = _call_gemini(system_text, outbound_q, model, ai_temperature, ai_timeout_s, ai_max_tokens)
            else:
                return {"ok": False, "reply": _("Unsupported provider."), "code": "E-PROV"}
        except Exception as e:
            _logger.error("Provider call error: %s", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("The assistant is temporarily unavailable. Please try again."), "code": "E-AI"}

        # Parse JSON contract (with fence stripping)
        ui: Dict[str, Any] = {
            "title": "",
            "summary": "",
            "answer_md": "",
            "citations": [],
            "suggestions": [],
        }
        parsed_ok = False
        try:
            cleaned = _strip_json_fences(raw)
            maybe = json.loads(cleaned)
            if isinstance(maybe, dict):
                ui.update({k: v for k, v in maybe.items() if k in ui})
                parsed_ok = True
        except Exception:
            parsed_ok = False

        # Fallback: treat raw as plain answer text
        if not parsed_ok:
            ui["answer_md"] = (raw or "").strip()

        # If model omitted citations but we have retrieval hits, add as "related sources"
        if not ui.get("citations") and snippets:
            cites = []
            for s in snippets[:5]:
                cites.append({"file": s.get("file"), "page": s.get("page")})
            ui["citations"] = cites

        # Enforce only_docs: require at least one citation; else guide the user
        if only_docs and not ui.get("citations"):
            ui.update({
                "title": _("I don’t know based on the current documents."),
                "summary": _("Please include the document number/code or try a different keyword."),
                "answer_md": "",
                "suggestions": [
                    _("Search by document number (e.g., INV-2024-00123)"),
                    _("Search by product code (e.g., ABC-123)"),
                ]
            })

        # Small heuristic suggestions
        if not ui.get("suggestions"):
            ui["suggestions"] = [
                _("Summarize this policy’s key points"),
                _("Where is this referenced in the docs?"),
            ]

        # Return final
        ms = int((time.time() - t0) * 1000)
        _logger.info("ai_chat ok=%s ms=%d provider=%s model=%s redactions=%d",
                     True, ms, provider, model, redactions)
        return {"ok": True, "reply": ui.get("summary") or ui.get("answer_md"), "ui": ui}
