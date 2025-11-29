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
import logging

_logger = logging.getLogger(__name__)


class AicApiKeyList(models.Model):
    """
    Master data: list of available API keys.
    """

    _name = "aic.api_key_list"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of API Keys"

    name = fields.Char(
        string="Identifier",
        required=True,
        tracking=True,
        help="API Key identifier (e.g. 'FirenorQMS').",
    )

    api_key = fields.Char(
        string="API Key",
        required=True,
        size=512,
        help="API key for the selected provider.\nKeep secret.",
    )

    file_store_ids = fields.One2many(
        comodel_name="aic.file_store_id",
        inverse_name="api_key_id",
        string="File Store IDs",
        help="File Store IDs linked to this API key for Gemini File Search.",
    )

    _sql_constraints = [
        (
            "aic_api_key_list_unique",
            "unique(api_key)",
            "Each API key must be unique in the list.",
        ),
    ]


class AicGeminiList(models.Model):
    """
    Master data: list of available Gemini model names.

    Example records:
        - aic_gemini_model = 'gemini-2.0-flash'
        - aic_gemini_model = 'gemini-2.0-pro'
    """

    _name = "aic.gemini_list"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of Gemini Models"
    _rec_name = "aic_gemini_model"
    _order = "aic_gemini_model"

    aic_gemini_model = fields.Char(
        string="Gemini Model",
        required=True,
        tracking=True,
        help="Gemini model identifier (e.g. 'gemini-2.0-flash-lite').",
    )

    _sql_constraints = [
        (
            "aic_gemini_model_unique",
            "unique(aic_gemini_model)",
            "Each Gemini model must be unique in the list.",
        ),
    ]


class AicFileStoreId(models.Model):
    """
    Master data: list of available File Store IDs.
    """

    _name = "aic.file_store_id"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of File Store IDs"

    name = fields.Char(
        string="Identifier",
        required=True,
        tracking=True,
        help="File Store ID Friendly name (e.g. 'FirenorQMS').",
    )

    file_store_id = fields.Char(
        string="File Store ID",
        required=True,
        size=256,
        help="File Store ID from Gemini (e.g. the FileSearchStore/FirenorQMS-9823u93jfjfenro).",
    )

    api_key_id = fields.Many2one(
        comodel_name="aic.api_key_list",
        string="API Key",
        required=True,
        ondelete="restrict",
        help="API key that owns this File Store ID.",
    )

    _sql_constraints = [
        (
            "aic_file_store_id_unique",
            "unique(file_store_id)",
            "Each File Store ID must be unique in the list.",
        ),
    ]


class AicGeminiSystemInstruction(models.Model):
    """
    Master data: named Gemini system instructions/personas.
    """

    _name = "aic.gemini_system_instruction"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "Gemini System Instructions"

    name = fields.Char(
        string="Name",
        required=True,
        tracking=True,
        help="Short name/label for this system instruction.",
    )

    gemini_system_instruction = fields.Text(
        string="Gemini System Instruction",
        help=(
            "Optional global system instruction (persona, behaviour, constraints) "
            "sent as system_instruction to Gemini in GenerateContentConfig."
        ),
    )

    _sql_constraints = [
        (
            "aic_gemini_system_instruction_unique",
            "unique(name)",
            "Each Gemini system instruction name must be unique in the list.",
        ),
    ]
