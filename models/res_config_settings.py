# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_provider = fields.Selection(
        selection=[("openai", "OpenAI"), ("gemini", "Google Gemini")],
        string="AI Provider",
        default="openai",
        config_parameter="website_ai_chat_min.provider",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="Select the AI provider to use for answering questions."
    )

    ai_api_key = fields.Char(
        string="AI API Key",
        config_parameter="website_ai_chat_min.api_key",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="API key for the selected AI provider."
    )

    ai_model = fields.Char(
        string="Model Name",
        default="gpt-4o-mini",
        config_parameter="website_ai_chat_min.model",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="Model identifier (e.g., 'gpt-4o-mini' or 'gemini-1.5-flash')."
    )

    docs_folder = fields.Char(
        string="Documents Folder (PDF)",
        config_parameter="website_ai_chat_min.docs_folder",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="Absolute path to a server folder containing PDF files used as context."
    )

    # CHANGED: Text -> Char (allowed in res.config.settings)
    sys_instruction = fields.Char(
        string="System Instructions",
        config_parameter="website_ai_chat_min.sys_instruction",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="Optional guardrails/instructions for the assistant."
    )

    # CHANGED: Text -> Char (allowed in res.config.settings)
    allowed_questions = fields.Char(
        string="Allowed Questions (Regex per line)",
        config_parameter="website_ai_chat_min.allowed_questions",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="If provided, only questions matching at least one regex are allowed. Leave empty to allow all."
    )

    context_only = fields.Boolean(
        string="Answer Only From Context",
        default=True,
        config_parameter="website_ai_chat_min.context_only",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="When checked, the assistant will refuse to answer if no relevant PDF context is found."
    )
