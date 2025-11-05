# -*- coding: utf-8 -*-
from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_provider = fields.Selection(
        selection=[("gemini", "Gemini (Google)"), ("openai", "OpenAI (Optional)")],
        default="gemini",
        string="AI Provider",
        config_parameter="website_ai_chat_min.provider",
        groups="website_ai_chat_min.group_ai_chat_admin",
    )
    ai_api_key = fields.Char(
        string="AI API Key",
        config_parameter="website_ai_chat_min.api_key",
        groups="website_ai_chat_min.group_ai_chat_admin",
    )
    ai_model = fields.Char(
        string="Model Name",
        default="gemini-2.0-flash-lite",
        config_parameter="website_ai_chat_min.model",
        groups="website_ai_chat_min.group_ai_chat_admin",
        help="Example: gemini-2.0-flash-lite or gpt-4o-mini",
    )
    ai_docs_folder = fields.Char(
        string="PDFs Folder (Server Path)",
        config_parameter="website_ai_chat_min.docs_folder",
        help="Absolute path on the Odoo server where PDFs live. Example: /opt/odoo/data/ai_pdfs",
        groups="website_ai_chat_min.group_ai_chat_admin",
    )
    ai_system_instruction = fields.Text(
        string="System Instruction / Guardrails",
        config_parameter="website_ai_chat_min.system_instruction",
        groups="website_ai_chat_min.group_ai_chat_admin",
        help="Ex: 'Answer only from the documents. If unsure, say I don't know.'",
    )
    ai_allowed_questions = fields.Text(
        string="Allowed Questions (regex per line)",
        config_parameter="website_ai_chat_min.allowed_questions",
        groups="website_ai_chat_min.group_ai_chat_admin",
        help="Optional. One regex per line. If provided, only matching questions will be answered.",
    )
    ai_context_only = fields.Boolean(
        string="Answer Only If Context Found",
        default=True,
        config_parameter="website_ai_chat_min.context_only",
        groups="website_ai_chat_min.group_ai_chat_admin",
        help="If enabled, bot refuses when no relevant evidence exists in the PDFs.",
    )
