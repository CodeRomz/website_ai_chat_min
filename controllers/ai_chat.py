# -*- coding: utf-8 -*-
"""
website_ai_chat_min controllers:
- /ai_chat/can_load : decides visibility (public or logged-in)
- /ai_chat/send     : handles messages, including doc-grounded replies

Features:
- Public toggle via ICP (enable_public / show_public / public_enabled)
- Group gate via ICP (require_group_xmlid) for logged-in visibility
- Rate limiting via ICP (rate_limit_max / rate_limit_window)
- Answer-only-from-docs via ICP (answer_only_from_docs + docs_folder)
- Optional regex allow-list via ICP (allowed_regex)
- Privacy URL returned to frontend via ICP (privacy_url)
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

import os
import re
import time
from typing import Dict, List, Tuple, Optional

# -------- Tunables (safe defaults; can be overridden via ICP) --------
DEFAULT_RATE_LIMIT_MAX = 5
DEFAULT_RATE_LIMIT_WINDOW = 15  # seconds
MAX_PDF_FILES = 50
MAX_PAGES_PER_PDF = 200
MAX_CHARS_PER_SNIPPET = 400
ALLOWED_EXTS = {".pdf"}

# Optional PDF libraries (best effort)
try:
    from pypdf import PdfReader  # pip install pypdf
except Exception:
    PdfReader = None  # type: ignore

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text  # pip install pdfminer.six
except Exception:
    pdfminer_extract_text = None  # type: ignore

# In-memory cache for doc index (per worker)
_DOC_CACHE: Dict[str, Dict] = {}
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "and", "or", "to",
    "is", "it", "for", "by", "with", "as", "at", "be", "this", "that",
}


# ------------------------------ Helpers --------------------------------
def _icp():
    return request.env["ir.config_parameter"].sudo()


def _get_icp_param(name, default=""):
    try:
        return _icp().get_param(name, default)
    except Exception:
        return default


def _get_bool_icp_any(names, default=False):
    for key in names:
        try:
            val = _icp().get_param(key, "")
            if val not in (None, "", False):
                return tools.str2bool(tools.ustr(val))
        except Exception:
            continue
    return bool(default)


def _get_int_icp(name, default: int) -> int:
    try:
        v = _icp().get_param(name, "")
        if not v:
            return default
        return int(str(v).strip())
    except Exception:
        return default


def _is_logged_in(env) -> bool:
    try:
        return env.user.has_group("base.group_user")
    except Exception:
        try:
            return env.user.id != env.ref("base.public_user").id
        except Exception:
            return False


def _require_group_if_configured(env) -> bool:
    xmlid = _get_icp_param("website_ai_chat_min.require_group_xmlid", "")
    if not xmlid:
        return True
    try:
        return env.user.has_group(xmlid)
    except Exception:
        _logger.warning("Invalid XMLID for group gate: %s", tools.ustr(xmlid))
        return False


def _session_rate_bucket() -> List[float]:
    try:
        bucket = request.session.get("ai_chat_rl", [])
        if not isinstance(bucket, list):
            bucket = []
        return bucket
    except Exception:
        return []


def _rate_check_and_bump() -> bool:
    limit = _get_int_icp("website_ai_chat_min.rate_limit_max", DEFAULT_RATE_LIMIT_MAX)
    window = _get_int_icp("website_ai_chat_min.rate_limit_window", DEFAULT_RATE_LIMIT_WINDOW)

    now = time.time()
    try:
        bucket = _session_rate_bucket()
        bucket = [t for t in bucket if (now - t) < window]
        if len(bucket) >= limit:
            request.session["ai_chat_rl"] = bucket
            return False
        bucket.append(now)
        request.session["ai_chat_rl"] = bucket
        return True
    except Exception:
        _logger.exception("Rate-limit check failed")
        return True


# ----------------------- PDF indexing & search --------------------------
def _safe_abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path or ""))


def _list_pdfs(folder: str) -> List[str]:
    files = []
    for root, _, fnames in os.walk(folder):
        for f in fnames:
            if os.path.splitext(f.lower())[1] in ALLOWED_EXTS:
                files.append(os.path.join(root, f))
        if len(files) >= MAX_PDF_FILES:
            break
    return sorted(files)[:MAX_PDF_FILES]


def _pdf_pages_pypdf(fp: str) -> List[str]:
    pages = []
    reader = PdfReader(fp)  # type: ignore
    n = min(len(reader.pages), MAX_PAGES_PER_PDF)
    for i in range(n):
        try:
            txt = reader.pages[i].extract_text() or ""
        except Exception:
            txt = ""
        if txt:
            pages.append(txt)
    return pages


def _pdf_text_pdfminer(fp: str) -> List[str]:
    # pdfminer returns full text; page breaks may be \f
    text = pdfminer_extract_text(fp) or ""  # type: ignore
    parts = [p.strip() for p in text.split("\f") if p.strip()]
    if not parts and text:
        parts = [text]
    return parts[:MAX_PAGES_PER_PDF]


def _read_pdf_pages(fp: str) -> List[str]:
    try:
        if PdfReader is not None:
            return _pdf_pages_pypdf(fp)
        if pdfminer_extract_text is not None:
            return _pdf_text_pdfminer(fp)
    except Exception as e:
        _logger.warning("PDF parse failed (%s): %s", fp, e)
    return []


def _load_docs_index(folder: str) -> Dict:
    """
    Cache structure:
    {
      'folder': '/abs/path',
      'files': {fp: mtime, ...},
      'pages': [(fp, page_no, text_lower, text_raw), ...],
      'scanned_at': ts
    }
    """
    folder = _safe_abs(folder)
    if not folder or not os.path.isdir(folder):
        raise ValidationError(_("Configured PDF folder does not exist: %s") % (folder or "<empty>"))

    # quick validity check on cache
    cached = _DOC_CACHE.get(folder)
    current_files = _list_pdfs(folder)
    current_state = {fp: os.path.getmtime(fp) for fp in current_files}

    need_scan = True
    if cached:
        if set(cached.get("files", {}).keys()) == set(current_state.keys()):
            # same files; check mtimes
            changed = any(cached["files"].get(fp) != current_state[fp] for fp in current_state)
            need_scan = changed
        else:
            need_scan = True

    if not need_scan and cached:
        return cached

    # (Re)scan
    pages_index = []
    for fp in current_files:
        try:
            pages = _read_pdf_pages(fp)
            for i, raw in enumerate(pages):
                raw = raw.strip()
                if not raw:
                    continue
                pages_index.append((fp, i + 1, raw.lower(), raw))
        except Exception as e:
            _logger.warning("Skip PDF due to read error (%s): %s", fp, e)

    idx = {
        "folder": folder,
        "files": current_state,
        "pages": pages_index,
        "scanned_at": time.time(),
    }
    _DOC_CACHE[folder] = idx
    _logger.info("[ai_chat] Indexed PDFs: %s files, %s pages", len(current_files), len(pages_index))
    return idx


def _tokenize(q: str) -> List[str]:
    toks = re.split(r"[^\w]+", (q or "").lower())
    return [t for t in toks if t and t not in _STOPWORDS]


def _best_snippet(text: str, width: int, tokens: List[str]) -> str:
    # try to center around first match
    tl = text.lower()
    for t in tokens:
        pos = tl.find(t)
        if pos >= 0:
            start = max(0, pos - width // 2)
            end = min(len(text), start + width)
            return text[start:end].strip()
    # fallback: first width chars
    return text[:width].strip()


def _search_docs_snippets(query: str, folder: str, topk: int = 3) -> List[str]:
    idx = _load_docs_index(folder)
    tokens = _tokenize(query)
    if not tokens:
        return []

    scored: List[Tuple[float, str]] = []  # (score, snippet)
    for (fp, page_no, low, raw) in idx["pages"]:
        score = 0.0
        for t in tokens:
            # simple frequency count
            score += low.count(t)
        if score <= 0:
            continue
        snippet = _best_snippet(raw, MAX_CHARS_PER_SNIPPET, tokens)
        label = f"[{os.path.basename(fp)} p.{page_no}] {snippet}"
        scored.append((score, label))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:topk]]


# ------------------------------- Routes --------------------------------
class WebsiteAIChatMin(http.Controller):

    @http.route("/ai_chat/can_load", type="json", auth="public", methods=["POST"], csrf=False)
    def can_load(self, **kw):
        try:
            env = request.env
            logged_in = _is_logged_in(env)
            enable_public = _get_bool_icp_any(
                [
                    "website_ai_chat_min.enable_public",
                    "website_ai_chat_min.show_public",
                    "website_ai_chat_min.public_enabled",
                ],
                default=False,
            )
            privacy_url = _get_icp_param("website_ai_chat_min.privacy_url", "")

            # Compute login redirect URL
            try:
                redirect_to = request.httprequest.url
            except Exception:
                redirect_to = "/"
            login_url = "/web/login?redirect=" + tools.url_quote(redirect_to)

            if not logged_in:
                show = bool(enable_public)
                _logger.info("[ai_chat.can_load] public=%s enable_public=%s show=%s",
                             True, enable_public, show)
                return {
                    "show": show,
                    "user_public": True,
                    "login_url": login_url,
                    "privacy_url": privacy_url,
                }

            # Logged-in gate
            passed_group_gate = _require_group_if_configured(env)
            show = bool(passed_group_gate)
            _logger.info("[ai_chat.can_load] public=%s passed_group_gate=%s show=%s",
                         False, passed_group_gate, show)
            return {
                "show": show,
                "user_public": False,
                "login_url": login_url,
                "privacy_url": privacy_url,
            }

        except Exception as e:
            _logger.error("can_load failed: %s", tools.ustr(e), exc_info=True)
            return {"show": False, "user_public": True, "login_url": "/web/login", "privacy_url": ""}

    @http.route("/ai_chat/send", type="json", auth="user", methods=["POST"], csrf=True)
    def send(self, **payload):
        try:
            if not _rate_check_and_bump():
                raise AccessError(_("You're sending messages too quickly. Please wait a moment."))

            prompt = tools.ustr((payload or {}).get("prompt", "")).strip()
            if not prompt:
                raise ValidationError(_("Please type a message."))

            # Optional allow-list regex (case-insensitive)
            allowed_regex = _get_icp_param("website_ai_chat_min.allowed_regex", "").strip()
            if allowed_regex:
                try:
                    rgx = re.compile(allowed_regex, flags=re.I | re.S)
                    if not rgx.search(prompt):
                        raise ValidationError(_("Your question is not allowed by policy."))
                except re.error as re_err:
                    _logger.warning("Invalid allowed_regex: %s", re_err)

            # Doc-grounding settings
            answer_only_from_docs = _get_bool_icp_any(["website_ai_chat_min.answer_only_from_docs"], default=False)
            docs_folder = _get_icp_param("website_ai_chat_min.docs_folder", "").strip()

            # Provider placeholders (wire your LLM here later)
            provider = _get_icp_param("website_ai_chat_min.ai_provider", "").strip()
            api_key = _get_icp_param("website_ai_chat_min.ai_api_key", "").strip()
            model = _get_icp_param("website_ai_chat_min.ai_model", "").strip()
            system_prompt = _get_icp_param("website_ai_chat_min.system_prompt", "").strip()

            snippets: List[str] = []
            if docs_folder:
                try:
                    snippets = _search_docs_snippets(prompt, docs_folder, topk=3)
                except Exception as e:
                    _logger.warning("Doc search error: %s", e)

            if answer_only_from_docs:
                if not snippets:
                    return {"ok": True, "reply": _("No relevant information found in the configured documents.")}
                # Minimal answer: return best snippets only
                return {"ok": True, "reply": "\n\n".join(snippets)}

            # If provider not configured, still return snippets (best-effort)
            if not provider or not api_key:
                if snippets:
                    return {
                        "ok": True,
                        "reply": _("Top matches from your documents:\n\n") + "\n\n".join(snippets),
                    }
                return {"ok": True, "reply": _("AI provider not configured. Please contact your administrator.")}

            # -----------------------------------------------------------------
            # Call your LLM provider here with (system_prompt, prompt, snippets)
            # For now, we just echo with snippets if available.
            # -----------------------------------------------------------------
            if snippets:
                reply = _("Based on your documents:\n\n") + "\n\n".join(snippets)
            else:
                reply = _("You said: %s") % prompt

            return {"ok": True, "reply": reply}

        except (ValidationError, AccessError) as e:
            return {"ok": False, "error": tools.ustr(getattr(e, "name", e))}
        except Exception as e:
            _logger.error("send failed: %s", tools.ustr(e), exc_info=True)
            return {"ok": False, "error": _("Unexpected error. Please try again later.")}
