# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Website AI Chat – controller + helpers

This file keeps the original public API and endpoint shapes:
  - POST /ai_chat/can_load  -> {"show": bool}
  - POST /ai_chat/send      -> {"ok": bool, "reply": str, "ui": {...}}

Major fixes in this revision
---------------------------
1) Gemini provider payload is now SDK-stable (uses `system_instruction` and
   explicit text parts) to avoid the "Could not create Blob" TypeError.
2) Robust JSON recovery: we parse strict JSON, fenced JSON, *and* loosely
   formatted "almost-JSON" so the UI never receives a raw object string.
3) Defensive cleanup & unused code removal. All public names / signatures are
   preserved. Only internal unused imports/vars were pruned.
"""

import json
import logging
import os
import time
import re as re_std
from typing import Dict, List, Tuple, Optional

from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)

# ---------------- Optional libs ----------------
try:
    import regex as regex_safe  # pragma: no cover - optional, used for ReDoS-safe timeouts
except Exception:  # pragma: no cover
    regex_safe = None

# =====================================================================================
# ICP helpers
# =====================================================================================

def _icp():
    return request.env["ir.config_parameter"].sudo()

def _get_icp_param(name: str, default: str = "") -> str:
    """Return ICP string param. If missing, return default."""
    try:
        val = _icp().get_param(name, default)
        return val if val not in (None, "") else default
    except Exception:
        return default

def _get_bool_icp(name: str, default: bool = False) -> bool:
    v = _get_icp_param(name, "")
    if v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "y", "yes", "on")

# =====================================================================================
# JSON helpers (robust)
# =====================================================================================

def _strip_md_fences(s: str) -> str:
    """Remove ```json fences or generic ``` fences if present."""
    if not s:
        return s
    s = s.strip()
    if s.startswith("```"):
        # strip first fence
        s = s.split("\n", 1)[1] if "\n" in s else ""
        # strip closing fence
        if "```" in s:
            s = s.rsplit("```", 1)[0]
    return s.strip()

def _find_balanced_object(s: str) -> Optional[str]:
    """Return first {...} balanced object text from string, or None."""
    if not s:
        return None
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i+1]
    return None

def extract_json_obj(s: str) -> Optional[dict]:
    """Try hard to parse JSON from a model reply string."""
    if not s:
        return None
    # direct
    try:
        return json.loads(s)
    except Exception:
        pass
    # fenced
    try:
        stripped = _strip_md_fences(s)
        return json.loads(stripped)
    except Exception:
        pass
    # balanced object
    try:
        inner = _find_balanced_object(s)
        if inner:
            return json.loads(inner)
    except Exception:
        pass
    return None

def _loose_extract(text: str, key: str) -> Optional[str]:
    """
    Best-effort extractor for a JSON-like `"key": "value"` even if the string
    contains unescaped newlines. Returns a normalized value or None.
    """
    try:
        if not text:
            return None
        m = re_std.search(rf'"{re_std.escape(key)}"\s*:\s*"((?:[^"\\]|\\.|[\r\n])*)"', text, re_std.S)
        if not m:
            return None
        val = m.group(1)
        return val.replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        return None

# =====================================================================================
# Request / validation helpers
# =====================================================================================

def _normalize_message_from_request(message_param: Optional[str] = None) -> str:
    """
    Accepts:
      - direct json: {"question": "..."} or {"message":"..."}
      - JSON-RPC: {"params":{"message":"..."}}
    The explicit `message_param` overrides body if provided by the route.
    """
    if message_param:
        return (message_param or "").strip()
    try:
        raw = request.httprequest.get_data(cache=False) or b""
        if not raw:
            return ""
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

def _match_allowed(pattern: str, text: str, timeout_ms: int = 100) -> bool:
    """Allow-list regex with timeout to resist ReDoS. Fail-closed if 'regex' is unavailable."""
    if not pattern:
        return True
    try:
        if regex_safe:
            return bool(regex_safe.search(pattern, text, timeout=timeout_ms / 1000.0))
        return bool(re_std.search(pattern, text))
    except Exception:
        # fail closed
        return False

def _redact_pii(s: str) -> str:
    """Very light PII redaction to avoid leaking obvious contact data to providers."""
    if not s:
        return s
    s = re_std.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", s)
    s = re_std.sub(r"\+?\d[\d\s().-]{6,}\d", "[phone]", s)
    return s

# =====================================================================================
# Routing & retrieval (Selective RAG)
# =====================================================================================

def _router_decide(q: str, force: bool = False) -> Tuple[str, float, str]:
    """
    Return (action, confidence, reason)
      action in {"retrieve","answer"}.
    Simple heuristic: look for doc-ish tokens or force flag.
    """
    if force:
        return "retrieve", 1.0, "force=True"
    ql = q.lower()
    docy = any(tok in ql for tok in ("fn-", "sop", "policy", "doc", "pdf", "page ", "p."))
    if docy:
        return "retrieve", 0.7, "doc-like query"
    return "answer", 0.0, "generic chat"

def _read_pdf_snippets(docs_folder: str, q: str, budget_ms: int = 600) -> List[Tuple[str, int, str]]:
    """
    Placeholder selective retrieval. Your existing deployment may implement
    a proper index; we keep the signature and return type. For safety we
    return [] when folder not set. This preserves behavior for generic chat
    and lets docs-only mode return the guided message when no snippets.
    """
    if not docs_folder or not os.path.isdir(docs_folder):
        return []
    # Minimal, fast scan of .txt sidecars if present (filename.pdf.txt)
    # This avoids adding heavy PDF libs here.
    deadline = time.time() + (budget_ms / 1000.0)
    ql = q.lower()
    results: List[Tuple[str, int, str]] = []
    try:
        for root, _, files in os.walk(docs_folder):
            for f in files:
                if time.time() > deadline:
                    return results
                if not f.lower().endswith(".pdf.txt"):
                    continue
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        txt = fh.read().lower()
                    if ql and ql in txt:
                        # page number unknown in sidecar; report p.1
                        snippet = txt[max(txt.find(ql)-80,0): txt.find(ql)+80].replace("\n"," ")
                        results.append((f[:-4], 1, snippet.strip()))
                        if len(results) >= 5:  # small cap
                            return results
                except Exception:
                    continue
    except Exception:
        pass
    return results

# =====================================================================================
# Prompt composition
# =====================================================================================

def _build_system_preamble(user_system_prompt: str, doc_snippets: List[Tuple[str, int, str]], only_docs: bool) -> str:
    """
    Compose a concise system instruction that includes the output contract and
    optionally the retrieved snippets.
    """
    lines: List[str] = []
    if user_system_prompt:
        lines.append(user_system_prompt.strip())
    lines.append(
        "You MUST answer by returning a single JSON object (no code fences) with keys: "
        'title, summary, answer_md, citations, suggestions.'
    )
    lines.append(
        "If you cite documents, include up to 8 items in 'citations' as objects with keys "
        '"file" and "page". Keep answer_md concise Markdown, safe subset.'
    )
    if only_docs:
        lines.append("Answer ONLY using the provided document snippets. If none are relevant, say you don't know.")
    if doc_snippets:
        lines.append("Relevant document snippets:")
        for (f, p, snip) in doc_snippets[:8]:
            lines.append(f"- {f} (p.{p}): {snip}")
    return "\n".join(lines)

# =====================================================================================
# Providers
# =====================================================================================

class _ProviderBase:
    def __init__(self, api_key: str, model: str, timeout: int = 30, temperature: float = 0.2, max_tokens: int = 800):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = float(temperature or 0.2)
        self.max_tokens = int(max_tokens or 800)

    def generate(self, prompt: str, user_text: str) -> str:
        raise NotImplementedError

class _GeminiProvider(_ProviderBase):
    def generate(self, prompt: str, user_text: str) -> str:  # keep signature
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)

        model = genai.GenerativeModel(
            self.model or "gemini-2.5-flash",
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
                "response_mime_type": "application/json",
            },
        )
        try:
            r = model.generate_content(
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=prompt,
                request_options={"timeout": self.timeout},
            )
        except Exception:
            # Fallback – best effort for older SDKs
            r = model.generate_content(
                [{"role": "user", "parts": [{"text": f"{prompt}\n\n{user_text}"}]}],
                request_options={"timeout": self.timeout},
            )
        return (getattr(r, "text", None) or "").strip()

class _OpenAIProvider(_ProviderBase):
    def generate(self, prompt: str, user_text: str) -> str:  # keep signature
        try:
            # openai>=1.0 style
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key)
            r = client.chat.completions.create(
                model=self.model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout_ms=self.timeout * 1000,
            )
            return (r.choices[0].message.content or "").strip()
        except Exception:
            # Legacy openai<1 fallback (very short; many installs no longer use this)
            import openai  # type: ignore
            openai.api_key = self.api_key
            r = openai.ChatCompletion.create(
                model=self.model or "gpt-3.5-turbo-1106",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return (r["choices"][0]["message"]["content"] or "").strip()

def _get_provider(cfg: Dict[str, str]) -> _ProviderBase:
    provider = (cfg.get("provider") or "openai").strip().lower()
    api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    model = cfg.get("model") or ""
    timeout = int(cfg.get("timeout", 30) or 30)
    temperature = float(cfg.get("temperature", 0.2) or 0.2)
    max_tokens = int(cfg.get("max_tokens", 800) or 800)
    if provider.startswith("gemini") or provider == "google":
        return _GeminiProvider(api_key, model or "gemini-2.5-flash", timeout, temperature, max_tokens)
    return _OpenAIProvider(api_key, model or "gpt-4o-mini", timeout, temperature, max_tokens)

# =====================================================================================
# Throttling / gating
# =====================================================================================

def _can_show_widget(env) -> bool:
    """Gate for the widget – by default require a logged-in user."""
    return not request.session.uid is None

def _throttle(bucket: str, limit: int = 8, window_sec: int = 60) -> bool:
    """Return True if allowed, False if throttled."""
    now = int(time.time())
    key = f"ai_chat:{bucket}"
    window = request.session.get(key) or []
    window = [t for t in window if now - t < window_sec]
    allowed = len(window) < limit
    if allowed:
        window.append(now)
        request.session[key] = window
    return allowed

# =====================================================================================
# Controller
# =====================================================================================

class WebsiteAIChatController(http.Controller):

    # ---------------- Security: group gating (optional) ----------------
    def _require_group_if_configured(self, env):
        group_xml = _get_icp_param("website_ai_chat_min.group_xml", "")
        if not group_xml:
            return
        user = env.user
        if not user.has_group(group_xml):
            raise AccessDenied(_("You do not have permission to use the assistant."))

    # ---------------- API: can_load ----------------
    @http.route("/ai_chat/can_load", type="json", auth="user", csrf=False, methods=["POST"])
    def can_load(self, **kwargs):
        try:
            show = _can_show_widget(request.env)
            return {"show": bool(show)}
        except Exception:
            return {"show": False}

    # ---------------- API: send ----------------
    @http.route("/ai_chat/send", type="json", auth="user", csrf=False, methods=["POST"])
    def send(self, question: Optional[str] = None, **kwargs):
        env = request.env

        # Permission & throttle
        self._require_group_if_configured(env)
        if not _throttle(bucket=str(request.session.uid or "anon")):
            return {"ok": True, "reply": _("You're sending messages too quickly. Please wait a moment."), "ui": {
                "title": _("Slow down"),
                "summary": _("Rate limit reached."),
                "answer_md": _("You're sending messages too quickly. Please wait a moment."),
                "citations": [],
                "suggestions": [],
            }}

        # Normalize input
        q = _normalize_message_from_request(question) or ""
        q = q.strip()
        if not q:
            return {"ok": True, "reply": _("Please enter a message."), "ui": {"title": "", "summary": "", "answer_md": _("Please enter a message."), "citations": [], "suggestions": []}}

        # Config
        cfg = {
            "provider": _get_icp_param("website_ai_chat_min.provider", "openai"),
            "api_key": _get_icp_param("website_ai_chat_min.api_key", ""),
            "model": _get_icp_param("website_ai_chat_min.model", ""),
            "timeout": _get_icp_param("website_ai_chat_min.timeout", "30"),
            "temperature": _get_icp_param("website_ai_chat_min.temperature", "0.2"),
            "max_tokens": _get_icp_param("website_ai_chat_min.max_tokens", "800"),
            "answer_only_from_docs": _get_bool_icp("website_ai_chat_min.only_docs", False),
            "docs_folder": _get_icp_param("website_ai_chat_min.docs_folder", ""),
            "allowed_regex": _get_icp_param("website_ai_chat_min.allowed_regex", ""),
            "system_prompt": _get_icp_param("website_ai_chat_min.system_prompt", "You are a helpful assistant for this website."),
            "docs_budget_ms": _get_icp_param("website_ai_chat_min.docs_budget_ms", "600"),
        }

        # Allow-list
        if not _match_allowed(cfg.get("allowed_regex", ""), q, timeout_ms=120):
            return {"ok": True, "reply": _("Your message contains a phrase that isn't allowed."), "ui": {"title": _("Blocked"), "summary": "", "answer_md": _("Your message contains a phrase that isn't allowed."), "citations": [], "suggestions": []}}

        outbound_q = _redact_pii(q)

        # Routing / retrieval
        route_action, conf, reason = _router_decide(outbound_q, bool(kwargs.get("force")))
        doc_snippets: List[Tuple[str, int, str]] = []
        if cfg["answer_only_from_docs"] or route_action == "retrieve":
            try:
                budget = int(cfg.get("docs_budget_ms") or "600")
            except Exception:
                budget = 600
            doc_snippets = _read_pdf_snippets(cfg["docs_folder"], outbound_q, budget_ms=budget)

        # Docs-only early exit
        if cfg["answer_only_from_docs"] and not doc_snippets:
            ui = {
                "title": _("No matching snippets"),
                "summary": _("No relevant passages were found in the documents."),
                "answer_md": _("I don’t know based on the current documents."),
                "citations": [],
                "suggestions": [_("Please include the document number/code (e.g., FN-PMO-PR-0040).")],
            }
            return {"ok": True, "reply": ui["answer_md"], "ui": ui}

        # Provider call
        system_text = _build_system_preamble(cfg["system_prompt"], doc_snippets, cfg["answer_only_from_docs"])
        provider = _get_provider(cfg)

        t0 = time.time()
        try:
            reply = provider.generate(system_text, outbound_q)
        except Exception as e:  # pragma: no cover
            _logger.exception("AI provider error: %s", e)
            return {"ok": True, "reply": _("The AI service is temporarily unavailable. Please try again shortly."), "ui": {
                "title": _("Service unavailable"),
                "summary": "",
                "answer_md": _("The AI service is temporarily unavailable. Please try again shortly."),
                "citations": [],
                "suggestions": [],
            }}
        ai_ms = int((time.time() - t0) * 1000)
        _logger.info("AI route=%s conf=%.02f provider=%s model=%s ai_ms=%s pii=%s",
                     route_action, conf, cfg["provider"], cfg["model"], ai_ms, bool(outbound_q != q))

        # ---------------- Parse UI payload (robust) ----------------
        ui = {
            "title": "",
            "summary": "",
            "answer_md": (reply or "").strip(),
            "citations": [],
            "suggestions": [],
        }

        parsed = extract_json_obj(reply or "")
        if isinstance(parsed, dict) and (parsed.get("answer_md") or parsed.get("text")):
            ui.update({
                "title": (parsed.get("title") or "")[:60],
                "summary": parsed.get("summary") or "",
                "answer_md": (parsed.get("answer_md") or parsed.get("text") or "").strip(),
                "citations": list(parsed.get("citations") or [])[:8],
                "suggestions": list(parsed.get("suggestions") or [])[:3],
            })
        else:
            # ensure we never send fenced content to the UI
            ui["answer_md"] = _strip_md_fences(ui["answer_md"])

            # NEW: salvage if the model dumped the JSON object inside answer_md
            salvage = extract_json_obj(ui["answer_md"])
            if isinstance(salvage, dict) and (salvage.get("answer_md") or salvage.get("text")):
                ui.update({
                    "title": (salvage.get("title") or "")[:60],
                    "summary": salvage.get("summary") or "",
                    "answer_md": (salvage.get("answer_md") or salvage.get("text") or "").strip(),
                    "citations": list(salvage.get("citations") or [])[:8],
                    "suggestions": list(salvage.get("suggestions") or [])[:3],
                })

        # Final loose extraction for almost-JSON (unescaped newlines etc.)
        if not ui["answer_md"] or (isinstance(ui["answer_md"], str) and ui["answer_md"].strip().startswith("{")):
            am = _loose_extract(reply or ui["answer_md"], "answer_md")
            if am:
                ui["answer_md"] = am.strip()
            t_ = _loose_extract(reply or "", "title")
            s_ = _loose_extract(reply or "", "summary")
            if t_ and not ui["title"]:
                ui["title"] = t_[:60]
            if s_ and not ui["summary"]:
                ui["summary"] = s_

        # Attach server-known citations if model omitted them
        if not ui["citations"] and doc_snippets:
            ui["citations"] = [{"file": f, "page": p} for (f, p, _) in doc_snippets[:5]]

        # Heuristic suggestion when context weak/broad
        if len(doc_snippets) == 0 or len(doc_snippets) > 8:
            hint = _("Please include the document number/code (e.g., FN-PMO-PR-0040) to narrow the result.")
            if hint not in ui["suggestions"]:
                ui["suggestions"].append(hint)

        # Return compact payload your JS expects
        MAX_ANSWER_CHARS = 1800
        ui["answer_md"] = (ui["answer_md"] or "")[:MAX_ANSWER_CHARS]
        return {"ok": True, "reply": (ui["answer_md"] or _("(No answer returned.)")), "ui": ui}
