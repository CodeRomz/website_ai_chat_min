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

_logger = logging.getLogger(__name__)

# ---------------- Defaults / Tunables (override via ICP) ----------------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

AI_DEFAULT_TIMEOUT = 15
AI_DEFAULT_TEMPERATURE = 0.2
AI_DEFAULT_MAX_TOKENS = 512

DOCS_DEFAULT_MAX_FILES = 40
DOCS_DEFAULT_MAX_PAGES = 40
DOCS_DEFAULT_MAX_HITS = 12
DOCS_DEFAULT_BUDGET_MS = 500  # time budget for scanning

ROUTER_RETRIEVE_T = 0.75
ROUTER_OFFER_T = 0.45

ASSISTANT_JSON_CONTRACT = """
Return ONLY a JSON object with:
{
  "title": string,           // ≤ 60 chars
  "summary": string,         // ≤ 80 words
  "answer_md": string,       // Markdown; ≤ 8 bullets OR ≤ 120 words
  "citations": [             // optional; from provided excerpts
    {"file": string, "page": integer}
  ],
  "suggestions": [string]    // optional; ≤ 3 follow-ups
}
Do NOT wrap the JSON in code fences.
Do NOT include text before or after the JSON.
If documents are insufficient and docs-only is enabled,
set "summary" to "I don’t know based on the current documents."
and keep "answer_md" short, asking the user to narrow by document number.
"""

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

def _require_group_if_configured(env) -> bool:
    xmlid = _get_icp_param("website_ai_chat_min.require_group_xmlid", "")
    if not xmlid:
        return True
    try:
        return env.user.has_group(xmlid)
    except Exception:
        return False  # fail-closed

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
    if force:
        return "retrieve", 1.0, "forced"
    s = _router_score(q)
    rt = float(_get_icp_param("website_ai_chat_min.router_retrieve_t", ROUTER_RETRIEVE_T))
    ot = float(_get_icp_param("website_ai_chat_min.router_offer_t", ROUTER_OFFER_T))
    if s >= rt:
        return "retrieve", s, f"score={s:.2f}"
    if ot <= s < rt:
        return "answer_with_offer", s, f"offer score={s:.2f}"
    return "answer", s, f"score={s:.2f}"

# ---------------- Negative cache (no-hit suppression) ----------------
def _neg_cache_key(q: str) -> str:
    return "ai:noc:" + str(hash(" ".join((q or "").lower().split())))

def _neg_cache_get(q: str) -> bool:
    return bool(request.session.get(_neg_cache_key(q)))

def _neg_cache_put(q: str) -> None:
    request.session[_neg_cache_key(q)] = int(time.time())
    request.session.modified = True

# ---------------- PDF retrieval (chunked, budgeted) ----------------
def _read_pdf_snippets(root_folder: str, query: str) -> List[Tuple[str, int, str]]:
    """
    Return list of (filename, page_index_1based, snippet_text).
    Walks subfolders; stops after ICP thresholds and budget; logs progress.
    """
    t0 = time.time()
    try:
        import pypdf
    except Exception as e:
        _logger.warning("pypdf not installed: %s", tools.ustr(e))
        return []

    max_files = _int_icp("website_ai_chat_min.docs_max_files", DOCS_DEFAULT_MAX_FILES)
    max_pages = _int_icp("website_ai_chat_min.docs_max_pages", DOCS_DEFAULT_MAX_PAGES)
    max_hits = _int_icp("website_ai_chat_min.docs_max_hits", DOCS_DEFAULT_MAX_HITS)
    budget_ms = _int_icp("website_ai_chat_min.docs_budget_ms", DOCS_DEFAULT_BUDGET_MS)

    results: List[Tuple[str, int, str]] = []
    ql = (query or "").lower()
    if not root_folder or not os.path.exists(root_folder):
        _logger.warning("Document folder not found or empty: %s", root_folder)
        return results

    files_scanned = 0
    budget_exhausted = False
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            if int((time.time() - t0) * 1000) > budget_ms:
                budget_exhausted = True
                break
            path = os.path.join(dirpath, fn)
            files_scanned += 1
            if files_scanned > max_files:
                break
            try:
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    page_count = min(len(reader.pages), max_pages)
                    for idx in range(page_count):
                        if int((time.time() - t0) * 1000) > budget_ms:
                            budget_exhausted = True
                            break
                        page = reader.pages[idx]
                        text = (page.extract_text() or "").strip()
                        if not text:
                            continue
                        tl = text.lower()
                        if ql in tl or any(tok in tl for tok in ql.split()[:3]):
                            pos = tl.find(ql) if ql in tl else 0
                            start = max(0, pos - 240)
                            end = min(len(text), pos + 240)
                            snippet = text[start:end].replace("\n", " ").strip()
                            results.append((fn, idx + 1, snippet))
                            if len(results) >= max_hits:
                                break
                    if len(results) >= max_hits or budget_exhausted:
                        break
            except Exception as e:
                _logger.warning("Failed to read PDF %s: %s", path, tools.ustr(e))
        if len(results) >= max_hits or budget_exhausted:
            break

    _logger.info(
        "[AIChat] Scan finished: files=%s hits=%s budget_ms=%s elapsed_ms=%d",
        files_scanned, len(results), budget_ms, int((time.time() - t0) * 1000),
    )
    return results[:max_hits]

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
                response_format={"type": "json_object"},
            )
            return (resp.choices[0].message.content or "").strip()
        return self._with_retries(_call)

class _GeminiProvider(_ProviderBase):
    def generate(self, system_text: str, user_text: str) -> str:
        def _call():
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=self.api_key)
            model_name = self.model or GEMINI_DEFAULT_MODEL
            prompt = (system_text + "\n\n" if system_text else "") + (user_text or "")
            r = genai.GenerativeModel(model_name).generate_content(
                [prompt],
                request_options={"timeout": self.timeout},
                generation_config={"temperature": self.temperature, "max_output_tokens": self.max_tokens},
            )
            return (getattr(r, "text", None) or "").strip()
        return self._with_retries(_call)

def _get_provider(cfg: dict) -> _ProviderBase:
    provider = (cfg.get("provider") or "openai").strip().lower()
    if provider == "gemini":
        return _GeminiProvider(cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"])
    return _OpenAIProvider(cfg["api_key"], cfg["model"], cfg["timeout"], cfg["temperature"], cfg["max_tokens"])

# ---------------- Prompt composition ----------------
def _build_system_preamble(system_prompt: str, snippets: List[Tuple[str, int, str]], only_docs: bool) -> str:
    lines = []
    base = (system_prompt or "").strip()
    if base:
        lines.append(base)
    if only_docs:
        lines.append(
            # "You MUST answer only using the provided excerpts. "
            # "If they don't contain the answer, say exactly: “I don’t know based on the current documents.”"
            "You Must answer accurately."
        )
    else:
        lines.append("Prefer the provided excerpts; be concise if you rely on general knowledge.")
    lines.append(
        "Formatting: Keep it compact. No more than 10 bullets or 200 words in 'answer_md'. "
        "Always include a short 'summary'. If many topics appear, ask for the document number/code."
    )
    lines.append("OUTPUT FORMAT:\n" + ASSISTANT_JSON_CONTRACT.strip())
    if snippets:
        lines.append("Relevant excerpts (cite using [File p.X]):")
        for fn, page, text in snippets:
            lines.append(f"[{fn} p.{page}] {text}")
    return "\n".join(lines)

# ---------------- Config loader ----------------
def _get_ai_config():
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

    retrieve_t = _float_icp("website_ai_chat_min.router_retrieve_t", ROUTER_RETRIEVE_T)
    offer_t = _float_icp("website_ai_chat_min.router_offer_t", ROUTER_OFFER_T)

    docs_budget_ms = _int_icp("website_ai_chat_min.docs_budget_ms", DOCS_DEFAULT_BUDGET_MS)

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
        "retrieve_t": retrieve_t,
        "offer_t": offer_t,
        "docs_budget_ms": docs_budget_ms,
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

def _extract_json_obj(text: str):
    """Be liberal in what we accept: strip fences and pick the first {...} block."""
    if not text:
        return None
    s = _strip_md_fences(text).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        candidate = s[i:j+1]
        try:
            return json.loads(candidate)
        except Exception:
            return None
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
    def send(self, question=None, force: bool = False):
        """
        Validates, selectively retrieves PDF context, composes prompt,
        calls provider with retries, and returns a compact, structured reply.
        """
        if not _require_group_if_configured(request.env):
            raise AccessDenied("You do not have access to AI Chat.")

        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        # Extract payload (accepts {question} or JSON-RPC envelope)
        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        # Pull explicit force flag (optional)
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

        # ---------------- Routing & Retrieval ----------------
        route_action, confidence, route_reason = _router_decide(q, force=force)
        doc_snippets: List[Tuple[str, int, str]] = []
        t_scan0 = time.time()

        docs_folder = cfg["docs_folder"]
        if (cfg["only_docs"] or route_action == "retrieve") and docs_folder and os.path.isdir(docs_folder):
            if not _neg_cache_get(q):
                doc_snippets = _read_pdf_snippets(docs_folder, q)
                if not doc_snippets:
                    _neg_cache_put(q)
        scan_ms = int((time.time() - t_scan0) * 1000)

        # Docs-only immediate response if no snippets
        if cfg["only_docs"] and not doc_snippets:
            ui = {
                "title": "",
                "summary": _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result."),
                "answer_md": _("I don’t know based on the current documents."),
                "citations": [],
                "suggestions": [_("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")],
            }
            return {"ok": True, "reply": ui["answer_md"], "ui": ui}

        # ---------------- Compose prompts ----------------
        system_text = _build_system_preamble(cfg["system_prompt"], doc_snippets, cfg["only_docs"])
        if route_action == "answer_with_offer":
            outbound_q += "\n\n(If helpful, I can check internal documents for the exact clause.)"

        # ---------------- Call provider ----------------
        provider = _get_provider(cfg)
        t_ai0 = time.time()
        try:
            reply = provider.generate(system_text, outbound_q)
        except Exception as e:
            _logger.error("[AIChat] Provider error (%s/%s): %s", cfg["provider"], cfg["model"] or "<default>", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("The AI service is temporarily unavailable. Please try again shortly.")}
        finally:
            ai_ms = int((time.time() - t_ai0) * 1000)
            _logger.info(
                "[AIChat] route=%s(%s) conf=%.2f provider=%s model=%s scan_ms=%s ai_ms=%s snippets=%s pii=%s",
                route_action, route_reason, confidence, cfg["provider"], cfg["model"] or "<default>",
                scan_ms, ai_ms, len(doc_snippets), bool(cfg["redact_pii"])
            )

        # ---------------- Parse UI payload (robust, CLEAN) ----------------
        ui = {
            "title": "",
            "summary": "",
            "answer_md": (reply or "").strip(),
            "citations": [],
            "suggestions": [],
        }

        parsed = _extract_json_obj(reply or "")
        if isinstance(parsed, dict) and parsed.get("answer_md"):
            ui.update({
                "title": (parsed.get("title") or "")[:60],
                "summary": parsed.get("summary") or "",
                "answer_md": (parsed.get("answer_md") or "").strip(),
                "citations": list(parsed.get("citations") or [])[:8],
                "suggestions": list(parsed.get("suggestions") or [])[:3],
            })
        else:
            # ensure we never send fenced content to the UI
            ui["answer_md"] = _strip_md_fences(ui["answer_md"])

        if not ui["citations"] and doc_snippets:
            ui["citations"] = [{"file": f, "page": p} for (f, p, _) in doc_snippets[:5]]

        # Heuristic suggestion when context weak/broad
        if len(doc_snippets) == 0 or len(doc_snippets) > 8:
            hint = _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")
            if hint not in ui["suggestions"]:
                ui["suggestions"].append(hint)

        # Return compact payload your JS expects
        return {"ok": True, "reply": (ui["answer_md"] or _("(No answer returned.)")), "ui": ui}
