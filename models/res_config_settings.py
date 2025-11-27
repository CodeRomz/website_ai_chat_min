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
