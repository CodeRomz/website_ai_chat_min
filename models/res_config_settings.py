# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import time
import mimetypes

from odoo import models, fields, api, tools, _
from odoo.exceptions import (
    UserError,
    ValidationError,
    RedirectWarning,
    AccessDenied,
    AccessError,
    CacheMiss,
    MissingError,
)

# Google GenAI (new SDK)
from google import genai
from google.genai import types  # kept for compatibility if you reference types elsewhere

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# MIME helpers (explicit types so we never depend on OS mime DB)
# ---------------------------------------------------------------------
_MIME_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    # add more if you need them:
    # ".csv": "text/csv",
    # ".json": "application/json",
    # ".xml": "application/xml",
    # ".zip": "application/zip",
}



def _normalize_store(name: str) -> str:
    """Ensure we always use a fully-qualified store resource name."""
    name = (name or "").strip()
    return name if (not name or name.startswith("fileSearchStores/")) else f"fileSearchStores/{name}"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ---------------------------------------------------------------------
    # Core provider/model/auth (left intact)
    # ---------------------------------------------------------------------
    ai_api_key = fields.Char(
        string="API Key",
        config_parameter="website_ai_chat_min.ai_api_key",
        help="API key for the selected provider.\nKeep secret.",
        size=512,
    )



    # ---------------------------------------------------------------------
    # Misc (kept)
    # ---------------------------------------------------------------------
    privacy_url = fields.Char(
        string="Privacy Policy URL",
        config_parameter="website_ai_chat_min.privacy_url",
        help="Optional URL to your privacy policy displayed in the chat UI.",
        size=1024,
    )

    # Optional features (kept)
    cache_enabled = fields.Boolean(
        string="Enable AI Chat Caching",
        config_parameter="website_ai_chat_min.cache_enabled",
        help="If enabled, the chat will cache document retrievals and computed replies "
        "to speed up repeated queries.",
        default=False,
    )


    # ---------------------------------------------------------------------
    # Gemini File Search
    # ---------------------------------------------------------------------

    file_store_id = fields.Char(
        string="File Store ID",
        config_parameter="website_ai_chat_min.file_store_id",
        help="File Store ID From Gemini",
        size=256,
    )



    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """Prefer the transient field, then ICP, then the environment."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        return (
            (self.ai_api_key or "").strip()
            or (ICP.get_param("website_ai_chat_min.ai_api_key") or "").strip()
            or (os.getenv("GEMINI_API_KEY") or "").strip()
        )