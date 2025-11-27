# -*- coding: utf-8 -*-

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

from odoo import http
from odoo.http import request


class AiChatController(http.Controller):
    """AI Chat controller.

    Step 1:
      - capture user question and log it
      - log whether the current user has an aic.admin configuration.
    """

    def _get_aic_admin_for_current_user(self):
        """
        Return the aic.admin record for the current request user, or None.

        This is read-only lookup, so sudo() is safe and avoids permission noise.
        """
        user = request.env.user
        try:
            if not user or not user.id:
                return None

            # In normal flows this is res.users; guard just in case.
            if user._name != "res.users":
                return None

            admin_rec = (
                request.env["aic.admin"]
                .sudo()
                .search(
                    [
                        ("aic_user_id", "=", user.id),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
            )
            return admin_rec or None

        except Exception as exc:
            _logger.exception(
                "AI Chat: error while looking up aic.admin for user_id=%s: %s",
                getattr(user, "id", None),
                exc,
            )
            return None

    @http.route(
        "/ai_chat/can_load",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def can_load(self, **kwargs):
        # For now: always allow mounting. We'll plug aic.admin checks here later.
        return {"show": True}

    @http.route(
        "/ai_chat/send",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def send(self, question=None, **kwargs):
        """Receive the question from JSON-RPC params and log it."""
        try:
            q = tools.ustr(question or "").strip()
            user = request.env.user if request and request.env else None

            # New: check if this user has an aic.admin config
            admin_rec = self._get_aic_admin_for_current_user()
            is_ai_user = bool(admin_rec)

            _logger.info(
                "AI Chat question: %r | user_id=%s | has_aic_admin=%s | aic_admin_id=%s",
                q,
                getattr(user, "id", None),
                is_ai_user,
                admin_rec.id if admin_rec else None,
            )

        except Exception as exc:
            _logger.exception("AI Chat: error while logging question: %s", exc)
            return {
                "ok": False,
                "reply": _("Error while logging your message."),
            }

        return {
            "ok": True,
            "reply": _("Your message has been logged."),
        }
