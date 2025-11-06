from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_provider = fields.Selection(
        [('openai', 'OpenAI'), ('gemini', 'Google Gemini')],
        string="AI Provider",
        default='openai',
        config_parameter='website_ai_chat_min.ai_provider',
        help="Select the AI provider to use for chat responses.",
    )

    ai_model = fields.Char(
        string="Model",
        default='gpt-4o-mini',
        config_parameter='website_ai_chat_min.ai_model',
        help="Exact model name supported by the selected provider.",
        size=128,
    )

    ai_api_key = fields.Char(
        string="API Key",
        config_parameter='website_ai_chat_min.ai_api_key',
        help="API key for the selected provider. Keep secret.",
        size=512,
    )

    # OWL-safe: use Char (Text is not allowed on res.config.settings)
    system_prompt = fields.Char(
        string="System Prompt",
        config_parameter='website_ai_chat_min.system_prompt',
        help="Optional system instructions prepended to every conversation.",
        size=4096,
    )

    allowed_regex = fields.Char(
        string="Allowed Questions (regex)",
        config_parameter='website_ai_chat_min.allowed_regex',
        help="Only allow questions that match this regular expression (case-insensitive). Leave empty to allow all.",
        size=1024,
    )

    docs_folder = fields.Char(
        string="PDF Folder",
        config_parameter='website_ai_chat_min.docs_folder',
        help="Absolute server path of a folder containing PDF documents used for grounding.",
        size=1024,
    )

    answer_only_from_docs = fields.Boolean(
        string="Answer Only From Documents",
        config_parameter='website_ai_chat_min.answer_only_from_docs',
        help="If enabled, the assistant answers only when relevant document snippets are found.",
    )

    privacy_url = fields.Char(
        string="Privacy Policy URL",
        config_parameter='website_ai_chat_min.privacy_url',
        help="Optional URL to your privacy policy displayed in the chat UI.",
        size=1024,
    )

    rate_limit_max = fields.Integer(
        string="Max requests per window",
        default=5,
        config_parameter='website_ai_chat_min.rate_limit_max',
        help="Max number of messages a user can send within the time window.",
    )

    rate_limit_window = fields.Integer(
        string="Window seconds",
        default=15,
        config_parameter='website_ai_chat_min.rate_limit_window',
        help="Duration of the throttle time window in seconds.",
    )

    @api.constrains('docs_folder')
    def _check_docs_folder(self):
        for rec in self:
            path = (rec.docs_folder or '').strip()
            if path and ('..' in path or path.startswith('~')):
                raise ValidationError(_("Invalid docs folder path."))
