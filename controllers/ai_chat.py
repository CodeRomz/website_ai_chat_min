# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from odoo import http
from odoo.http import request

import json
import os
import time
import re as re_std
from typing import Dict, List, Tuple

try:
    import regex as regex_safe  # type: ignore
except Exception:
    regex_safe = None

# ---- Defaults / tunables (can be overridden via ICP) -------------------------
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
Do NOT include text before/after the JSON.
If documents are insufficient and docs-only is enabled,
set "summary" to "I don’t know based on the current documents."
and keep "answer_md" short, asking the user to narrow by document number.
"""

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
        return False


def _can_show_widget(env) -> bool:
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
    try:
        xfwd = request.httprequest.headers.get("X-Forwarded-For", "")
        ip = (xfwd.split(",")[0].strip() if xfwd else request.httprequest.remote_addr) or "0.0.0.0"
        return ip
    except Exception:
        return "0.0.0.0"


def _throttle() -> bool:
    """Simple per-session/IP sliding-window throttle."""
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
        return True


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


def _match_allowed(pattern: str, text: str, timeout_ms=60) -> bool:
    """Allow-list regex with timeout to resist ReDoS."""
    if not pattern:
        return True
    try:
        if regex_safe:
            return bool(regex_safe.search(pattern, text, flags=regex_safe.I | regex_safe.M, timeout=timeout_ms))
        return bool(re_std.search(pattern, text, flags=re_std.I | re_std.M))
    except Exception as e:
        _logger.warning("Invalid allowed_regex or match error: %s", tools.ustr(e))
        return False


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


def _read_pdf_snippets(root_folder: str, query: str) -> List[Tuple[str, int, str]]:
    """
    Return list of (filename, page_index_1based, snippet_text).
    Walks subfolders; stops after ICP thresholds; logs progress.
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

    results: List[Tuple[str, int, str]] = []
    ql = (query or "").lower()

    if not root_folder or not os.path.exists(root_folder):
        _logger.warning("Document folder not found or empty: %s", root_folder)
        return results

    _logger.info("[AIChat] Scanning folder=%s query=%r (limits: files=%s pages=%s hits=%s)",
                 root_folder, query, max_files, max_pages, max_hits)

    files_scanned = 0
    for dirpath, _, filenames in os.walk(root_folder):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(dirpath, fn)
            files_scanned += 1
            if files_scanned > max_files:
                _logger.info("[AIChat] File ceiling reached at %s files", max_files)
                break
            try:
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    page_count = min(len(reader.pages), max_pages)
                    for idx in range(page_count):
                        page = reader.pages[idx]
                        text = (page.extract_text() or "").strip()
                        if not text:
                            continue
                        if ql in text.lower():
                            snippet = text[:1200].replace("\n", " ")
                            results.append((fn, idx + 1, snippet))
                            _logger.info("[AIChat] Match: %s (p.%s)", fn, idx + 1)
                            if len(results) >= max_hits:
                                raise StopIteration
            except StopIteration:
                break
            except Exception as e:
                _logger.warning("[AIChat] Failed to read PDF %s: %s", path, tools.ustr(e))
        else:
            continue
        break

    _logger.info("[AIChat] Scan finished: files_scanned=%s hits=%s scan_ms=%d",
                 files_scanned, len(results), int((time.time() - t0) * 1000))
    return results


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
    }


def _redact_pii(text: str) -> str:
    """Minimal PII masking; configurable on/off."""
    try:
        if not text:
            return text
        text = re_std.sub(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"***@***", text)  # emails
        text = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "***", text)  # phones (rough)
        text = re_std.sub(r"\b[A-Za-z0-9]{8,12}\b", "***", text)  # simple IDs
        return text
    except Exception:
        return text


# -----------------------------------------------------------------------------
# Provider adapters
# -----------------------------------------------------------------------------
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
                else:
                    break
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


def _build_system_preamble(system_prompt: str, snippets: List[Tuple[str, int, str]], only_docs: bool) -> str:
    lines = []
    base = (system_prompt or "").strip()
    if base:
        lines.append(base)

    if only_docs:
        lines.append(
            "You MUST answer only using the provided excerpts. "
            "If they don't contain the answer, say exactly: “I don’t know based on the current documents.”"
        )
    else:
        lines.append("Prefer the provided excerpts; be concise if you rely on general knowledge.")

    # Formatting contract (strict/brevity)
    lines.append(
        "Formatting: Keep it compact. No more than 8 bullets or 120 words in 'answer_md'. "
        "Always include a short 'summary'. If many topics appear, ask for the document number/code."
    )
    lines.append("OUTPUT FORMAT:\n" + ASSISTANT_JSON_CONTRACT.strip())

    if snippets:
        lines.append("Relevant excerpts (cite using [File p.X]):")
        for fn, page, text in snippets:
            lines.append(f"[{fn} p.{page}] {text}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# HTTP Controller
# -----------------------------------------------------------------------------
class WebsiteAIChatController(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", csrf=False, methods=["POST"])
    def can_load(self):
        try:
            show = _can_show_widget(request.env)
            _logger.info("[website_ai_chat_min] can_load show=%s", show)
            return {"show": bool(show)}
        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False}

    @http.route("/ai_chat/send", type="json", auth="user", csrf=True, methods=["POST"])
    def send(self, question=None):
        """
        Validates, (optionally) retrieves PDF context, composes prompt,
        calls provider with retries, and returns a compact, structured reply.
        """
        if not _require_group_if_configured(request.env):
            raise AccessDenied("You do not have access to AI Chat.")

        if not _throttle():
            return {"ok": False, "reply": _("Please wait a moment before sending another message.")}

        q = _normalize_message_from_request(question_param=question)
        if not q:
            return {"ok": False, "reply": _("Please enter a question.")}
        if len(q) > 4000:
            return {"ok": False, "reply": _("Question too long (max 4000 chars).")}

        try:
            _logger.info("[website_ai_chat_min] /ai_chat/send uid=%s len=%s ip=%s",
                         request.env.uid, len(q), _client_ip())
        except Exception:
            pass

        # --- Load configuration
        cfg = _get_ai_config()
        if not cfg["api_key"]:
            return {"ok": False, "reply": _("AI provider API key is not configured. Please contact the administrator.")}

        if cfg["allowed_regex"] and not _match_allowed(cfg["allowed_regex"], q):
            return {"ok": False, "reply": _("Your question is not within the allowed scope.")}

        # --- Redact PII if configured (for outbound prompt only)
        outbound_q = _redact_pii(q) if cfg["redact_pii"] else q

        # --- Document retrieval
        doc_snippets: List[Tuple[str, int, str]] = []
        t_scan0 = time.time()
        if cfg["docs_folder"] and os.path.isdir(cfg["docs_folder"]):
            doc_snippets = _read_pdf_snippets(cfg["docs_folder"], q)
        scan_ms = int((time.time() - t_scan0) * 1000)

        if cfg["only_docs"] and not doc_snippets:
            _logger.info("[AIChat] only_docs enabled and no snippets found; scan_ms=%s", scan_ms)
            ui = {
                "title": "",
                "summary": _("I don’t know based on the current documents."),
                "answer_md": _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result."),
                "citations": [],
                "suggestions": [_("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")],
            }
            return {"ok": True, "reply": ui["answer_md"], "ui": ui}

        # --- Build prompts
        system_text = _build_system_preamble(cfg["system_prompt"], doc_snippets, cfg["only_docs"])
        user_text = outbound_q

        # --- Call provider
        provider = _get_provider(cfg)
        t_ai0 = time.time()
        try:
            reply = provider.generate(system_text, user_text)
        except Exception as e:
            _logger.error("[AIChat] Provider error (%s/%s): %s",
                          cfg["provider"], cfg["model"] or "<default>", tools.ustr(e), exc_info=True)
            return {"ok": False, "reply": _("The AI service is temporarily unavailable. Please try again shortly.")}
        finally:
            ai_ms = int((time.time() - t_ai0) * 1000)
            _logger.info("[AIChat] provider=%s model=%s scan_ms=%s ai_ms=%s snippets=%s pii_redacted=%s",
                         cfg["provider"], cfg["model"] or "<default>", scan_ms, ai_ms, len(doc_snippets), bool(cfg["redact_pii"]))

        # --- Parse structured UI payload (JSON), with graceful fallback
        ui = {
            "title": "",
            "summary": "",
            "answer_md": (reply or "").strip(),
            "citations": [],
            "suggestions": [],
        }
        try:
            parsed = json.loads(reply or "")
            if isinstance(parsed, dict) and parsed.get("answer_md"):
                ui.update({
                    "title": (parsed.get("title") or "")[:120],
                    "summary": parsed.get("summary") or "",
                    "answer_md": (parsed.get("answer_md") or "").strip(),
                    "citations": list(parsed.get("citations") or [])[:8],
                    "suggestions": list(parsed.get("suggestions") or [])[:3],
                })
        except Exception:
            # not JSON; keep raw text
            pass

        # Heuristic suggestions if context weak/too broad
        if len(doc_snippets) == 0 or len(doc_snippets) > 8:
            doc_hint = _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")
            if doc_hint not in ui["suggestions"]:
                ui["suggestions"].append(doc_hint)

        # Ensure citations exist (from retrieval) if model omitted them
        if not ui["citations"] and doc_snippets:
            ui["citations"] = [{"file": f, "page": p} for (f, p, _) in doc_snippets[:5]]

        # Docs-only enforcement if model returned empty
        if cfg["only_docs"] and not (ui["answer_md"] or "").strip():
            ui["summary"] = _("I don’t know based on the current documents.")
            ui["answer_md"] = _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")

        return {"ok": True, "reply": (ui["answer_md"] or _("(No answer returned.)")), "ui": ui}
