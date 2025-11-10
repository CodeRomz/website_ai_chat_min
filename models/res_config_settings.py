from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from google.generativeai import genai
from google import types
import time


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
        help="Only allow questions that match this regular expression (case-insensitive).\nLeave empty to allow all.",
        size=1024,
    )

    docs_folder = fields.Char(
        string="PDF Folder",
        config_parameter='website_ai_chat_min.docs_folder',
        help="Absolute server path of a folder containing PDF documents used for grounding.",
        size=1024,
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

    # NEW: Enable caching of frequently asked questions and document searches.
    cache_enabled = fields.Boolean(
        string="Enable AI Chat Caching",
        config_parameter='website_ai_chat_min.cache_enabled',
        help="If enabled, the chat will cache document retrievals and computed replies to speed up repeated queries.",
        default=False,
    )

    # NEW: Use advanced routing heuristics.
    advanced_router_enabled = fields.Boolean(
        string="Enable Advanced Routing",
        config_parameter='website_ai_chat_min.advanced_router_enabled',
        help="If enabled, a more sophisticated routing algorithm will decide when to consult internal documents, using weighted keyword analysis. Disable to use the legacy router.",
        default=False,
    )

    # NEW: Gemini File Search integration
    file_search_enabled = fields.Boolean(
        string="Enable Gemini File Search",
        config_parameter='website_ai_chat_min.file_search_enabled',
        help=(
            "When using the Gemini provider, enable retrieval augmented generation via "
            "File Search.  This offloads document retrieval to Google's API instead of "
            "scanning local PDFs.  Requires specifying a File Search store name below."
        ),
        default=False,
    )

    file_search_store = fields.Char(
        string="File Search Store Name",
        config_parameter='website_ai_chat_min.file_search_store',
        help=(
            "The fully-qualified FileSearchStore resource name (e.g., "
            "'fileSearchStoresName').  This store must be created and loaded "
            "with your documents via the Gemini API."
        ),
        size=256,
    )

    file_search_index = fields.Char(
        string="File Search Index File",
        config_parameter='website_ai_chat_min.file_search_index',
        help=(
            "Index file where the gemini file search will initialize first."
        ),
        size=256,
    )

    @api.constrains('docs_folder')
    def _check_docs_folder(self):
        for rec in self:
            path = (rec.docs_folder or '').strip()
            if path and ('..' in path or path.startswith('~')):
                raise ValidationError(_("Invalid docs folder path."))

    def _file_search_index_sync(self):
        client = genai.Client()

        # Create the file search store with an optional display name
        file_search_store = client.file_search_stores.create(config={'display_name': self.file_search_store})

        # Upload and import a file into the file search store, supply a file name which will be visible in citations
        operation = client.file_search_stores.upload_to_file_search_store(
            file=self.file_search_index,
            file_search_store_name=file_search_store.name,
            config={
                'display_name': self.file_search_store,
            }
        )




