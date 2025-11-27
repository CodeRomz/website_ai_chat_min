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


class aic_admin(models.Model):
    """
    Per-user AI chat configuration.

    One record per user, with child lines defining per-model limits.
    The model name 'aic.admin' is prefixed with 'aic.' to avoid conflicts
    with other modules.
    """

    _name = "aic.admin"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "Chat Admin"
    _rec_name = "user_id"
    _order = "user_id"

    active = fields.Boolean(
        string="Active",
        default=True,
        tracking=True,
        help="If disabled, limits for this user will be ignored.",
    )

    user_id = fields.Many2one(
        comodel_name="res.users",
        string="User",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
        help="User who is allowed to use the AI chat with configured limits.",
    )

    line_ids = fields.One2many(
        comodel_name="aic.user_quota_line",
        inverse_name="quota_id",
        string="Model Limits",
        help="Per-model AI chat limits for this user.",
    )

    _sql_constraints = [
        (
            "aic_admin_user_unique",
            "unique(user_id)",
            "There is already an AI chat configuration record for this user.",
        ),
    ]

    @api.model
    def get_user_model_limits(self, user, model_name):
        """
        Central helper for the chat logic to read limits for a given user+model.

        :param user: res.users record OR integer user_id
        :param model_name: technical model name (e.g. 'res.partner')
        :return: dict {'prompt_limit': int, 'tokens_per_prompt': int} or None
        """
        try:
            if isinstance(user, models.BaseModel):
                user_id = user.id
            else:
                user_id = int(user)
        except (TypeError, ValueError) as exc:
            _logger.error(
                "Invalid user argument for get_user_model_limits: %s (%s)",
                user,
                exc,
            )
            return None

        result = None
        try:
            if not user_id or not model_name:
                return None

            admin_rec = self.search(
                [("user_id", "=", user_id), ("active", "=", True)],
                limit=1,
            )
            if not admin_rec:
                return None

            # Filter on related technical name for speed.
            line = admin_rec.line_ids.filtered(
                lambda l: l.model_technical_name == model_name
                or l.model_id.model == model_name
            )[:1]

            if not line:
                return None

            result = {
                "prompt_limit": line.prompt_limit,
                "tokens_per_prompt": line.tokens_per_prompt,
            }
        except Exception as exc:
            _logger.exception(
                "Error fetching AI chat limits for user %s, model %s: %s",
                user_id,
                model_name,
                exc,
            )
            result = None
        finally:
            # Kept for future extension (metrics, audit logging, etc.).
            return result


class aic_user_quota_line(models.Model):
    """
    Per-model limits attached to aic.admin.

    The model name 'aic.user_quota_line' is prefixed with 'aic.' to avoid
    conflicts with other modules.
    """

    _name = "aic.user_quota_line"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "AI Chat Per Model Limits"
    _order = "model_id"

    active = fields.Boolean(
        string="Active",
        default=True,
        tracking=True,
        help="If disabled, this specific model limit will not be enforced.",
    )

    quota_id = fields.Many2one(
        comodel_name="aic.admin",
        string="Chat Admin Config",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
        help="The AI chat admin configuration this line belongs to.",
    )

    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        required=True,
        ondelete="restrict",
        domain=[("transient", "=", False)],
        tracking=True,
        help="Odoo business model the user can use in the chat "
             "(e.g. 'res.partner').",
    )

    model_technical_name = fields.Char(
        string="Technical Model Name",
        related="model_id.model",
        store=True,
        readonly=True,
        help="Cached technical model name for faster lookups "
             "from the chat logic.",
    )

    prompt_limit = fields.Integer(
        string="Prompt Limit",
        required=True,
        default=0,
        tracking=True,
        help=(
            "Maximum number of prompts the user can send for this model. "
            "Your chat logic can interpret this as per day, per month, etc."
        ),
    )

    tokens_per_prompt = fields.Integer(
        string="Tokens per Prompt",
        required=True,
        default=0,
        tracking=True,
        help="Maximum allowed LLM tokens per prompt for this model.",
    )

    _sql_constraints = [
        (
            "aic_user_quota_line_unique_model",
            "unique(quota_id, model_id)",
            "You already defined AI chat limits for this model "
            "for the selected user.",
        ),
    ]

    @api.constrains("prompt_limit", "tokens_per_prompt")
    def _check_non_negative_limits(self):
        """Ensure prompt/token limits are not negative."""
        for line in self:
            if line.prompt_limit < 0:
                raise ValidationError(
                    _(
                        "Prompt limit for model '%(model)s' cannot be negative.",
                        model=line.model_id.display_name,
                    )
                )
            if line.tokens_per_prompt < 0:
                raise ValidationError(
                    _(
                        "Tokens per prompt for model '%(model)s' "
                        "cannot be negative.",
                        model=line.model_id.display_name,
                    )
                )
