from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from google.generativeai import genai
import time
import os



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

    # ----------------------------
    # Helpers
    # ----------------------------
    def _resolve_api_key(self) -> str:
        """Prefer the transient field, then ICP, then the environment."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        return (
            (self.ai_api_key or "").strip()
            or (ICP.get_param("website_ai_chat_min.ai_api_key") or "").strip()
            or (os.getenv("GEMINI_API_KEY") or "").strip()
        )

    # ----------------------------
    # Admin button: Sync Index
    # ----------------------------
    def file_search_index_sync(self):
        """
        Upload exactly one file located at <docs_folder>/<file_search_index>
        to Gemini File Search. Reuse store if present; otherwise create one.
        Polls the indexing operation to completion and shows a toast.
        """
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()

        # Provider guard (stay consistent with your runtime switch)
        provider = (self.ai_provider or ICP.get_param("website_ai_chat_min.ai_provider") or "").strip()
        if provider != "gemini":
            raise UserError(_("Set AI Provider to 'Gemini' to use File Search."))

        # API key
        api_key = self._resolve_api_key()
        if not api_key:
            raise UserError(_("Set the Gemini API Key (or GEMINI_API_KEY env) before syncing."))

        # Paths
        docs_root = (self.docs_folder or ICP.get_param("website_ai_chat_min.docs_folder") or "").strip()
        rel_name = (self.file_search_index or ICP.get_param("website_ai_chat_min.file_search_index") or "").strip()

        if not docs_root:
            raise UserError(_("Configure 'PDF Folder' (docs_folder) in Settings."))
        if not rel_name:
            raise UserError(_("Set 'File Search Index File' (relative to docs_folder)."))

        abs_root = os.path.abspath(docs_root)
        abs_path = os.path.abspath(os.path.normpath(os.path.join(abs_root, rel_name)))

        # basic traversal safety + existence
        if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
            raise UserError(_("Unsafe path. The index file must be inside the 'PDF Folder'."))
        if not os.path.isfile(abs_path):
            raise UserError(_("File not found or not a file: %s") % abs_path)

        # Client
        client = genai.Client(api_key=api_key)

        # Resolve or create store
        store_name = (self.file_search_store or ICP.get_param("website_ai_chat_min.file_search_store") or "").strip()
        if not store_name:
            store = client.file_search_stores.create(config={"display_name": "odoo-kb"})
            store_name = store.name
            ICP.set_param("website_ai_chat_min.file_search_store", store_name)
            self.file_search_store = store_name  # reflect immediately

        # Upload + index the single file (keep display name user-friendly)
        op = client.file_search_stores.upload_to_file_search_store(
            file=abs_path,
            file_search_store_name=store_name,
            config={"display_name": os.path.basename(abs_path)},
        )

        # Poll the LRO (max ~5 minutes)
        start = time.time()
        while not getattr(op, "done", False):
            time.sleep(2)
            if time.time() - start > 300:
                raise UserError(_("Indexing timed out; please retry or check server logs."))
            op = client.operations.get(op)

        # Success toast
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Gemini File Search"),
                "message": _("Indexed: %s → %s") % (os.path.basename(abs_path), store_name),
                "sticky": False,
                "type": "success",
            },
        }



