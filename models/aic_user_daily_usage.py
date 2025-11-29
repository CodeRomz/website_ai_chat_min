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
        string="User",
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
                elif (
                    aic_model._name == "aic.user_quota_line"
                    and aic_model.aic_model_id
                ):
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
                getattr(model_rec, "id", None),
                exc,
            )
            # Fail-open
            return True, None

        return True, usage_rec
