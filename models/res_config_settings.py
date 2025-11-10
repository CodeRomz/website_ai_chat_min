from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging
_logger = logging.getLogger(__name__)

from google import genai
from google.genai import types
import time
import os
import mimetypes

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def _guess_mime(path: str) -> str:
    """Return a safe MIME type for the given path or raise a clear error."""
    ext = os.path.splitext(path)[1].lower()
    m = _MIME_MAP.get(ext) or mimetypes.guess_type(path)[0]
    if not m:
        # Fallback to octet-stream is possible, but better to fail loudly so you know what to upload.
        raise UserError(
            _("Unsupported or unknown file type for: %s. Use .pdf, .docx, .md, or .txt (or extend the MIME map).")
            % path
        )
    return m

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

    # ----------------------------
    # Validations
    # ----------------------------
    @api.constrains("docs_folder")
    def _check_docs_folder(self):
        for rec in self:
            path = (rec.docs_folder or "").strip()
            if not path:
                continue
            if path.startswith("~") or ".." in path:
                raise ValidationError(_("Invalid docs folder path. Use an absolute, safe path."))

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

        # Paths (use realpath to defeat symlink escapes)
        docs_root = (self.docs_folder or ICP.get_param("website_ai_chat_min.docs_folder") or "").strip()
        rel_name = (self.file_search_index or ICP.get_param("website_ai_chat_min.file_search_index") or "").strip()

        if not docs_root:
            raise UserError(_("Configure 'PDF Folder' (docs_folder) in Settings."))
        if not rel_name:
            raise UserError(_("Set 'File Search Index File' (relative to docs_folder)."))

        real_root = os.path.realpath(docs_root)
        real_path = os.path.realpath(os.path.join(real_root, rel_name))

        if not (real_path == real_root or real_path.startswith(real_root + os.sep)):
            raise UserError(_("Unsafe path. The index file must be inside the 'PDF Folder'."))
        if not os.path.isfile(real_path):
            raise UserError(_("File not found or not a file: %s") % real_path)

        # Preflight size (Gemini limit ~100 MB per file)
        size_mb = os.path.getsize(real_path) / (1024 * 1024)
        if size_mb > 100:
            raise UserError(_("The file is %.1f MB which exceeds the 100 MB limit.") % size_mb)

        # Client
        client = genai.Client(api_key=api_key)

        # Resolve or create store
        store_name = (self.file_search_store or ICP.get_param("website_ai_chat_min.file_search_store") or "").strip()
        if not store_name:
            store = client.file_search_stores.create(config={"display_name": "odoo-kb"})
            store_name = store.name
            ICP.set_param("website_ai_chat_min.file_search_store", store_name)
            self.file_search_store = store_name  # reflect immediately

        # Determine MIME type (validated for logging & guardrails; SDK infers during upload)
        mime_type = _guess_mime(real_path)

        _logger.info("Gemini File Search: uploading %s (mime=%s) to store %s", real_path, mime_type, store_name)

        # Upload + index with chunking + metadata
        # NOTE: Do NOT pass mime_type here; the SDK does not accept it on this method.
        # (1) Upload the raw file to Files API with explicit mime_type
        uploaded = client.files.upload(
            file=real_path,
            config={
                # Visible in citations (Files API uses 'name' here)
                "name": os.path.basename(real_path),
                # Critical: set MIME here to avoid guessing failures
                "mime_type": mime_type,
            },
        )

        # (2) Import the uploaded File into the File Search Store
        # import_file supports custom_metadata and chunking_config directly.
        op = client.file_search_stores.import_file(
            file_search_store_name=store_name,
            file_name=uploaded.name,  # e.g., 'files/abc-123'
            custom_metadata=[
                {"key": "source", "string_value": os.path.basename(real_path)},
                # keep/add any other tags you use (e.g., tenant)
                # {"key": "tenant", "string_value": self.env.cr.dbname},
            ],
            chunking_config={
                "white_space_config": {"max_tokens_per_chunk": 400, "max_overlap_tokens": 40}
            },
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
                "message": _("Indexed: %s → %s") % (os.path.basename(real_path), store_name),
                "sticky": False,
                "type": "success",
            },
        }