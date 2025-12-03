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

    Used to enforce aic_prompt_limit (prompts/day) without storing any chat
    content on the server.
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

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @api.model
    def _normalize_prompt_limit(self, prompt_limit):
        """Return a safe integer limit; <= 0 means 'unlimited'."""
        try:
            limit_int = int(prompt_limit) if prompt_limit is not None else 0
        except (TypeError, ValueError):
            limit_int = 0
        return limit_int

    @api.model
    def _resolve_model_record(self, aic_model):
        """Resolve aic.gemini_list record from record or raw model code.

        Any internal error is logged and None is returned so callers can
        apply a fail-closed policy.
        """
        Model = self.env["aic.gemini_list"].sudo()
        model_rec = None

        try:
            if isinstance(aic_model, models.BaseModel):
                if aic_model._name == "aic.gemini_list":
                    model_rec = aic_model
                elif (
                    aic_model._name == "aic.user_quota_line"
                    and getattr(aic_model, "aic_model_id", False)
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
            model_rec = None

        return model_rec

    # -------------------------------------------------------------------------
    # Public API – check vs increment
    # -------------------------------------------------------------------------

    @api.model
    def check_prompt_allowed(self, aic_user_rec, aic_model, prompt_limit):
        """
        Check whether a user is allowed to send another prompt today.

        * DOES NOT increment the counter.
        * Fail-closed: unexpected internal errors return (False, None)
          so callers block the prompt and avoid unbounded usage.

        :param aic_user_rec: aic.user record (required)
        :param aic_model: aic.gemini_list record or raw model code string
        :param prompt_limit: int from aic.user_quota_line
                             0 / None / <=0 => unlimited (always allowed)
        :return: (allowed: bool, usage_rec: record or None)
        """
        limit_int = self._normalize_prompt_limit(prompt_limit)

        # Unlimited prompts => no counter, always allowed
        if limit_int <= 0:
            return True, None

        if not aic_user_rec or not getattr(aic_user_rec, "id", False):
            _logger.warning(
                "AI Chat: usage check without valid aic.user record "
                "(model=%s)",
                aic_model,
            )
            return False, None

        model_rec = self._resolve_model_record(aic_model)
        if not model_rec:
            # Fail-closed: configuration inconsistency should not grant
            # unlimited usage.
            _logger.error(
                "AI Chat: usage check with unknown model '%s' for aic.user %s – "
                "denying.",
                aic_model,
                getattr(aic_user_rec, "id", None),
            )
            return False, None

        Usage = self.sudo()
        usage_date = fields.Date.context_today(self)

        try:
            usage_rec = Usage.search(
                [
                    ("aic_user_id", "=", aic_user_rec.id),
                    ("aic_model_id", "=", model_rec.id),
                    ("usage_date", "=", usage_date),
                ],
                limit=1,
            )
            if usage_rec and usage_rec.prompts_used >= limit_int:
                return False, usage_rec
            return True, usage_rec
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while checking daily usage for aic.user %s, "
                "model %s: %s",
                getattr(aic_user_rec, "id", None),
                getattr(model_rec, "id", None),
                exc,
            )
            # Fail-closed: if we cannot reliably read usage, do not allow.
            return False, None

    @api.model
    def increment_prompt_usage(
        self,
        aic_user_rec,
        aic_model,
        prompt_limit,
        usage_rec=None,
    ):
        """
        Persist a single used prompt for the given user+model+date.

        Assumes `check_prompt_allowed()` was already called and returned
        allowed=True for the same arguments.

        Internal errors are logged and swallowed to avoid breaking the
        user-facing response once a reply has been generated.

        :param aic_user_rec: aic.user record (required)
        :param aic_model: aic.gemini_list record or raw model code string
        :param prompt_limit: int from aic.user_quota_line
                             0 / None / <=0 => unlimited (no-op)
        :param usage_rec: optional existing aic.user_daily_usage record
        :return: updated/created usage_rec or None on failure
        """
        limit_int = self._normalize_prompt_limit(prompt_limit)

        # Unlimited prompts => nothing to persist
        if limit_int <= 0:
            return None

        if not aic_user_rec or not getattr(aic_user_rec, "id", False):
            _logger.warning(
                "AI Chat: increment_prompt_usage without valid aic.user "
                "(model=%s)",
                aic_model,
            )
            return None

        model_rec = self._resolve_model_record(aic_model)
        if not model_rec:
            _logger.error(
                "AI Chat: increment_prompt_usage with unknown model '%s' "
                "for aic.user %s.",
                aic_model,
                getattr(aic_user_rec, "id", None),
            )
            return None

        Usage = self.sudo()
        usage_date = fields.Date.context_today(self)

        try:
            # Ensure usage_rec matches this user+model+date; otherwise drop it.
            if (
                usage_rec
                and (
                    usage_rec.aic_user_id.id != aic_user_rec.id
                    or usage_rec.aic_model_id.id != model_rec.id
                    or usage_rec.usage_date != usage_date
                )
            ):
                usage_rec = None

            if usage_rec:
                usage_rec.write(
                    {"prompts_used": usage_rec.prompts_used + 1}
                )
            else:
                usage_rec = Usage.search(
                    [
                        ("aic_user_id", "=", aic_user_rec.id),
                        ("aic_model_id", "=", model_rec.id),
                        ("usage_date", "=", usage_date),
                    ],
                    limit=1,
                )
                if usage_rec:
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
                "AI Chat: error while incrementing daily usage for aic.user %s, "
                "model %s: %s",
                getattr(aic_user_rec, "id", None),
                getattr(model_rec, "id", None),
                exc,
            )
            return None

        return usage_rec

    # -------------------------------------------------------------------------
    # Backward-compat wrapper – keeps old call sites working
    # -------------------------------------------------------------------------

    @api.model
    def check_and_increment_prompt(self, aic_user_rec, aic_model, prompt_limit):
        """
        Backward-compatible wrapper kept for existing callers.

        New code should call `check_prompt_allowed()` and
        `increment_prompt_usage()` separately so quota is consumed only
        after a successful Gemini reply.

        Returns (allowed: bool, usage_rec: record or None).
        """
        allowed, usage_rec = self.check_prompt_allowed(
            aic_user_rec=aic_user_rec,
            aic_model=aic_model,
            prompt_limit=prompt_limit,
        )
        if not allowed:
            return False, usage_rec

        usage_rec = self.increment_prompt_usage(
            aic_user_rec=aic_user_rec,
            aic_model=aic_model,
            prompt_limit=prompt_limit,
            usage_rec=usage_rec,
        )
        return True, usage_rec
