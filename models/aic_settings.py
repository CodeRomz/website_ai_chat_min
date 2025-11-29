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
    _description = "List of Api Keys"

    name = fields.Char(
        string="Identifier",
        help="API Key identifier (e.g. 'FirenorQMS').",
    )

    api_key = fields.Char(
        string="API Key",
        help="API key for the selected provider.\nKeep secret.",
        size=512,
    )

    _sql_constraints = [
        (
            "aic_api_key_list_unique",
            "unique(aic_api_key_list_model)",
            "Each Api Keys must be unique in the list.",
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
    Master data: list of available API keys.

    """
    _name = "aic.file_store_id"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of File Store ID"
    _rec_name = "file_store_id"

    file_store_id = fields.Char(
        string="File Store ID",
        help="File Store ID from Gemini (e.g. the FileSearchStore identifier).",
        size=256,
    )

    _sql_constraints = [
        (
            "aic_file_store_id_unique",
            "unique(aic_file_store_id_model)",
            "Each File Store ID must be unique in the list.",
        ),
    ]

class AicFileStoreIdGroup(models.Model):
    """
    Master data: list of available API keys.

    """
    _name = "aic.file_store_id_group"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of File Store ID"
    _rec_name = "file_store_id_group"

    file_store_id_group = fields.Char(
        string="File Store ID Group",
        help="File Store ID Group is to help identify FileSearchStore (e.g. FNO-QMS, FNI-Project, MM-BDD).",
        size=256,
    )

    _sql_constraints = [
        (
            "aic_file_store_id_group_unique",
            "unique(aic_file_store_id_group_model)",
            "Each File Store ID Group must be unique in the list.",
        ),
    ]

class AicGeminiSystemInstruction(models.Model):
    """
    Master data: list of available API keys.

    """
    _name = "aic.gemini_system_instruction"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "List of File Store ID"

    name = fields.Char(
        string="Name",
        required=True,
        tracking=True,
    )

    gemini_system_instruction = fields.Text(
        string="Gemini System Instruction",
        help=(
            "Optional global system instruction (persona, behaviour, constraints) "
            "sent as system_instruction to Gemini in GenerateContentConfig."
        ),
        size=10000,  # allow a long persona string
    )

    _sql_constraints = [
        (
            "aic_gemini_system_instruction_unique",
            "unique(aic_gemini_system_instruction_model)",
            "Each File Store ID must be unique in the list.",
        ),
    ]




