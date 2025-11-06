# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Provider
    ai_provider = fields.Selection(
        selection=[("openai", "OpenAI"), ("gemini", "Google Gemini"), ("azure", "Azure OpenAI"), ("other", "Other")],
        string="AI Provider",
        config_parameter="website_ai_chat_min.ai_provider",
    )
    ai_model = fields.Char(
        string="Model",
        config_parameter="website_ai_chat_min.ai_model",
    )
    ai_api_key = fields.Char(
        string="API Key",
        config_parameter="website_ai_chat_min.ai_api_key",
    )

    # Visibility
    public_enabled = fields.Boolean(
        string="Show AI Chat to Public",
        config_parameter="website_ai_chat_min.enable_public",
        help="If enabled, anonymous visitors will see the bubble. Sending still requires login.",
    )

    # Policy / Prompting
    system_prompt = fields.Text(
        string="System Prompt",
        config_parameter="website_ai_chat_min.system_prompt",
    )
    allowed_regex = fields.Char(
        string="Allowed Questions (regex)",
        config_parameter="website_ai_chat_min.allowed_regex",
        help="Case-insensitive regex; only matching prompts are accepted.",
    )
    answer_only_from_docs = fields.Boolean(
        string="Answer Only From Documents",
        config_parameter="website_ai_chat_min.answer_only_from_docs",
    )

    # Documents
    docs_folder = fields.Char(
        string="PDF Folder",
        config_parameter="website_ai_chat_min.docs_folder",
        help="Absolute server path containing PDF files for grounding answers.",
    )

    # UX / Legal
    privacy_url = fields.Char(
        string="Privacy Policy URL",
        config_parameter="website_ai_chat_min.privacy_url",
    )

    # Rate limiting
    rate_limit_max = fields.Integer(
        string="Rate Limit: Max Requests",
        config_parameter="website_ai_chat_min.rate_limit_max",
        default=5,
    )
    rate_limit_window = fields.Integer(
        string="Rate Limit: Window (sec)",
        config_parameter="website_ai_chat_min.rate_limit_window",
        default=15,
    )
