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
    # ... existing code unchanged ...
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


class AicUser(models.Model):
    # ... existing code unchanged ...
    _name = "aic.user"
    _inherit = ["mail.activity.mixin", "mail.thread"]
    _description = "AI Chat user"
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
            "aic_user_unique",
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
    Per-Gemini-model limits attached to aic.user.
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
        comodel_name="aic.user",
        string="AI Chat User Config",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
        help="The AI chat user configuration this line belongs to.",
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
        help="Max prompts per calendar day for this user+model. "
             "0 or None means no daily limit.",
    )

    aic_tokens_per_prompt = fields.Integer(
        string="Tokens per Prompt",
        required=True,
        default=0,
        tracking=True,
        help="Max output tokens per answer for this user+model. "
             "If 0, a safe default is used in the backend.",
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


class AicUserDailyUsage(models.Model):
    """
    Daily usage counters per (aic.user, Gemini model, calendar date).

    This is used to enforce aic_prompt_limit (prompts/day) without
    storing any chat content on the server.
    """

    _name = "aic.user_daily_usage"
    _description = "AI Chat Daily Usage per User & Model"
    _order = "usage_date desc, aic_user_id, aic_model_id"

    aic_user_id = fields.Many2one(
        "aic.user",
        string="AI Chat User Config",
        required=True,
        ondelete="cascade",
        index=True,
        help="AI chat user configuration this usage entry belongs to.",
    )

    aic_model_id = fields.Many2one(
        "aic.gemini_list",
        string="Gemini Model",
        required=True,
        ondelete="cascade",
        index=True,
        help="Gemini model this usage entry refers to.",
    )

    usage_date = fields.Date(
        string="Usage Date",
        required=True,
        index=True,
        help="Logical 24h window (calendar date in user timezone).",
    )

    prompts_used = fields.Integer(
        string="Prompts Used",
        default=0,
        help="Number of prompts sent that date for this user+model.",
    )

    _sql_constraints = [
        (
            "aic_user_daily_usage_unique",
            "unique(aic_user_id, aic_model_id, usage_date)",
            "Daily usage record must be unique per AI user, model and date.",
        ),
    ]

    @api.model
    def check_and_increment_prompt(self, aic_user_rec, aic_model, prompt_limit):
        """
        Enforce daily prompt limit per user+model.

        - aic_user_rec: aic.user record (must be valid)
        - aic_model: aic.gemini_list record OR raw model name string
        - prompt_limit: int from aic.user_quota_line
            - 0, None or <=0 = unlimited (no blocking)

        Returns: (allowed: bool, usage_rec: record or None)

        Fail-open policy: on unexpected errors, allow the prompt
        but log the exception so SRE can investigate.
        """
        # Normalise prompt_limit
        try:
            prompt_limit_int = int(prompt_limit) if prompt_limit is not None else 0
        except (TypeError, ValueError):
            prompt_limit_int = 0

        # Unlimited prompts => no counter, always allowed
        if prompt_limit_int <= 0:
            return True, None

        if not aic_user_rec or not getattr(aic_user_rec, "id", False):
            # Should not happen because /ai_chat/send already checks this
            _logger.warning(
                "AI Chat: usage check without valid aic.user record (model=%s)",
                aic_model,
            )
            return False, None

        # Resolve model record
        model_rec = None
        Model = self.env["aic.gemini_list"].sudo()
        try:
            if isinstance(aic_model, models.BaseModel):
                if aic_model._name == "aic.gemini_list":
                    model_rec = aic_model
                elif aic_model._name == "aic.user_quota_line" and aic_model.aic_model_id:
                    model_rec = aic_model.aic_model_id
            else:
                code = tools.ustr(aic_model or "").strip()
                if code:
                    model_rec = Model.search(
                        [("aic_gemini_model", "=", code)],
                        limit=1,
                    )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error resolving model for daily usage (%s): %s",
                aic_model,
                exc,
            )
            # Fail-open
            return True, None

        if not model_rec:
            # If model is unknown here but passed aic.user checks, don't block.
            _logger.warning(
                "AI Chat: usage check called with unknown model '%s' – allowing.",
                aic_model,
            )
            return True, None

        usage_date = fields.Date.context_today(self)
        Usage = self.sudo()

        try:
            usage_rec = Usage.search(
                [
                    ("aic_user_id", "=", aic_user_rec.id),
                    ("aic_model_id", "=", model_rec.id),
                    ("usage_date", "=", usage_date),
                ],
                limit=1,
            )
            if usage_rec:
                if usage_rec.prompts_used >= prompt_limit_int:
                    return False, usage_rec
                usage_rec.write(
                    {"prompts_used": usage_rec.prompts_used + 1}
                )
            else:
                usage_rec = Usage.create(
                    {
                        "aic_user_id": aic_user_rec.id,
                        "aic_model_id": model_rec.id,
                        "usage_date": usage_date,
                        "prompts_used": 1,
                    }
                )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while updating daily usage for aic.user %s, "
                "model %s: %s",
                getattr(aic_user_rec, "id", None),
                model_rec.id,
                exc,
            )
            # Fail-open
            return True, None

        return True, usage_rec
