from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Provider & core
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
        help="API key for the selected provider.\nKeep secret.",
        size=512,
    )
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

    # Docs / retrieval
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
    docs_max_files = fields.Integer(
        string="Docs: Max Files",
        default=40,
        config_parameter='website_ai_chat_min.docs_max_files',
        help="Ceiling for number of PDF files scanned per request.",
    )
    docs_max_pages = fields.Integer(
        string="Docs: Max Pages/File",
        default=40,
        config_parameter='website_ai_chat_min.docs_max_pages',
        help="Ceiling for number of pages read per file.",
    )
    docs_max_hits = fields.Integer(
        string="Docs: Max Matches",
        default=12,
        config_parameter='website_ai_chat_min.docs_max_hits',
        help="Ceiling for number of matched snippets sent to the AI.",
    )

    # Inference controls
    ai_timeout = fields.Integer(
        string="AI Timeout (sec)",
        default=15,
        config_parameter='website_ai_chat_min.ai_timeout',
        help="HTTP/client timeout for provider calls.",
    )
    ai_temperature = fields.Float(
        string="AI Temperature",
        default=0.2,
        config_parameter='website_ai_chat_min.ai_temperature',
        help="Creativity: lower is more focused.",
    )
    ai_max_tokens = fields.Integer(
        string="AI Max Tokens",
        default=512,
        config_parameter='website_ai_chat_min.ai_max_tokens',
        help="Maximum output tokens per answer.",
    )
    redact_pii = fields.Boolean(
        string="Redact PII in Prompts",
        default=False,
        config_parameter='website_ai_chat_min.redact_pii',
        help="Mask emails/phones/IDs before sending the user's message to the AI.",
    )

    # Access/rate limits
    privacy_url = fields.Char(
        string="Privacy Policy URL",
        config_parameter='website_ai_chat_min.privacy_url',
        help="Optional URL to your privacy policy displayed in the chat UI.",
        size=1024,
    )
    require_group_xmlid = fields.Char(
        string="Require Group (XMLID)",
        config_parameter='website_ai_chat_min.require_group_xmlid',
        help="If set, only users in this group can see/use the chat. Example: base.group_user",
        size=256,
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

    # Validations
    @api.constrains('docs_folder')
    def _check_docs_folder(self):
        for rec in self:
            path = (rec.docs_folder or '').strip()
            if path and ('..' in path or path.startswith('~')):
                raise ValidationError(_("Invalid docs folder path."))

    @api.constrains('ai_timeout', 'ai_temperature', 'ai_max_tokens',
                    'docs_max_files', 'docs_max_pages', 'docs_max_hits',
                    'rate_limit_max', 'rate_limit_window')
    def _check_ranges(self):
        for r in self:
            if r.ai_timeout and (r.ai_timeout < 5 or r.ai_timeout > 120):
                raise ValidationError(_("AI Timeout must be between 5 and 120 seconds."))
            if r.ai_temperature is not None and (r.ai_temperature < 0.0 or r.ai_temperature > 1.0):
                raise ValidationError(_("AI Temperature must be between 0.0 and 1.0."))
            for name, val, lo, hi in [
                ("AI Max Tokens", r.ai_max_tokens, 64, 4096),
                ("Docs: Max Files", r.docs_max_files, 1, 500),
                ("Docs: Max Pages/File", r.docs_max_pages, 1, 200),
                ("Docs: Max Matches", r.docs_max_hits, 1, 100),
                ("Rate Limit: Max", r.rate_limit_max, 1, 100),
                ("Rate Limit: Window", r.rate_limit_window, 5, 600),
            ]:
                if val and (val < lo or val > hi):
                    raise ValidationError(_("%s must be between %s and %s.") % (name, lo, hi))
