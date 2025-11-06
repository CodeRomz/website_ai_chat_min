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
        config_parameter="website_ai_chat_min.ai_provider",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
    )
    ai_api_key = fields.Char(
        string="AI API Key",
        config_parameter="website_ai_chat_min.ai_api_key",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
    )
    ai_model = fields.Char(
        string="AI Model",
        default="gpt-3.5-turbo",
        config_parameter="website_ai_chat_min.ai_model",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="OpenAI: e.g., gpt-3.5-turbo, gpt-4.  Gemini: e.g., gemini-pro",
    )
    docs_folder = fields.Char(
        string="Documents Folder (PDF)",
        config_parameter="website_ai_chat_min.docs_folder",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="Absolute path to a folder containing PDF documents for grounding.",
    )
    sys_instruction = fields.Text(
        string="System Instruction (Optional)",
        config_parameter="website_ai_chat_min.sys_instruction",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="An optional system prompt to guide the AI’s tone and constraints.",
    )
    allowed_questions = fields.Text(
        string="Allowed Questions (Regex, one per line)",
        config_parameter="website_ai_chat_min.allowed_questions",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="If set, only questions matching at least one regex are processed.",
    )
    context_only = fields.Boolean(
        string="Answer Only From Documents",
        default=False,
        config_parameter="website_ai_chat_min.context_only",
        groups="base.group_system,website_ai_chat_min.group_ai_chat_admin",
        help="If enabled, the assistant will answer only if relevant PDF context is found.",
    )
