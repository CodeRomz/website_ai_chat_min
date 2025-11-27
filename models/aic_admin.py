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


class AicAdmin(models.Model):
    """
    Per-user AI chat configuration.

    One record per user, with child lines defining per-Gemini-model limits.
    """

    _name = "aic.admin"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "Chat Admin"
    _rec_name = "aic_user_id"
    _order = "aic_user_id"

    active = fields.Boolean(
        string="Active",
        default=True,
        tracking=True,
        help="If disabled, limits for this user will be ignored.",
    )

    aic_user_id = fields.Many2one(
        comodel_name="res.users",
        string="User",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
        help="User who is allowed to use the AI chat with configured limits.",
    )

    aic_line_ids = fields.One2many(
        comodel_name="aic.user_quota_line",
        inverse_name="aic_quota_id",
        string="Model Limits",
        help="Per-Gemini-model AI chat limits for this user.",
    )

    _sql_constraints = [
        (
            "aic_admin_user_unique",
            "unique(aic_user_id)",
            "There is already an AI chat configuration record for this user.",
        ),
    ]

    @api.model
    def get_user_model_limits(self, user, model_name):
        """
        Read limits for a given (user, Gemini model).

        :param user: res.users record OR integer user_id
        :param model_name: Gemini model string,
                           e.g. 'gemini-2.0-flash-lite'
        :return: dict {'prompt_limit': int, 'tokens_per_prompt': int} or None
        """
        result = None

        # Normalize user -> ID
        try:
            if isinstance(user, models.BaseModel):
                aic_user_id = user.id
            else:
                aic_user_id = int(user)
        except (TypeError, ValueError) as exc:
            _logger.error(
                "Invalid user argument for get_user_model_limits: %s (%s)",
                user,
                exc,
            )
            return None

        # Normalize model_name -> string (Gemini code)
        if isinstance(model_name, models.BaseModel) and model_name._name == "aic.gemini_list":
            gemini_code = model_name.aic_gemini_model
        else:
            gemini_code = str(model_name or "").strip()

        line = self.env["aic.user_quota_line"]
        try:
            if not aic_user_id or not gemini_code:
                return None

            admin_rec = self.search(
                [("aic_user_id", "=", aic_user_id), ("active", "=", True)],
                limit=1,
            )
            if not admin_rec:
                return None

            # Match on the Gemini model string stored in aic.gemini_list
            line = admin_rec.aic_line_ids.filtered(
                lambda l: l.aic_model_id
                and l.aic_model_id.aic_gemini_model == gemini_code
            )[:1]

        except Exception as exc:
            _logger.exception(
                "Error fetching AI chat limits for user %s, model %s: %s",
                aic_user_id,
                gemini_code,
                exc,
            )
        else:
            if line:
                result = {
                    "prompt_limit": line.aic_prompt_limit,
                    "tokens_per_prompt": line.aic_tokens_per_prompt,
                }
        finally:
            return result


class AicUserQuotaLine(models.Model):
    """
    Per-Gemini-model limits attached to aic.admin.
    """

    _name = "aic.user_quota_line"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "AI Chat Per Gemini Model Limits"
    _order = "aic_model_id"

    active = fields.Boolean(
        string="Active",
        default=True,
        tracking=True,
        help="If disabled, this specific model limit will not be enforced.",
    )

    aic_quota_id = fields.Many2one(
        comodel_name="aic.admin",
        string="Chat Admin Config",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
        help="The AI chat admin configuration this line belongs to.",
    )

    aic_model_id = fields.Many2one(
        comodel_name="aic.gemini_list",
        string="Gemini Model",
        required=True,
        ondelete="restrict",
        tracking=True,
        help="Gemini Chat model (e.g. 'gemini-2.0-flash-lite').",
    )

    aic_prompt_limit = fields.Integer(
        string="Prompt Limit",
        required=True,
        default=0,
        tracking=True,
        help=(
            "Maximum number of prompts the user can send for this model. "
            "Your chat logic can interpret this as per day, per month, etc."
        ),
    )

    aic_tokens_per_prompt = fields.Integer(
        string="Tokens per Prompt",
        required=True,
        default=0,
        tracking=True,
        help="Maximum allowed LLM tokens per prompt for this model.",
    )

    _sql_constraints = [
        (
            "aic_user_quota_line_unique_model",
            "unique(aic_quota_id, aic_model_id)",
            "You already defined AI chat limits for this Gemini model "
            "for the selected user.",
        ),
    ]

    @api.constrains("aic_prompt_limit", "aic_tokens_per_prompt")
    def _check_non_negative_limits(self):
        """Ensure prompt/token limits are not negative."""
        for line in self:
            if line.aic_prompt_limit < 0:
                raise ValidationError(
                    _(
                        "Prompt limit for model '%(model)s' cannot be negative.",
                        model=line.aic_model_id.aic_gemini_model,
                    )
                )
            if line.aic_tokens_per_prompt < 0:
                raise ValidationError(
                    _(
                        "Tokens per prompt for model '%(model)s' "
                        "cannot be negative.",
                        model=line.aic_model_id.aic_gemini_model,
                    )
                )
