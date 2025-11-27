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
    """Step 1: only capture user question and log it.
    No AI / Gemini / quota logic yet.
    """

    @http.route(
        "/ai_chat/can_load",
        type="json",
        auth="public",
        methods=["POST"],
        csrf=True,
    )
    def can_load(self, **kwargs):
        # For now: always allow mounting. We'll plug aic.admin later.
        return {"show": True}

    @http.route(
        "/ai_chat/send",
        type="json",
        auth="public",
        methods=["POST"],
        csrf=True,
    )
    def send(self, question=None, **kwargs):
        """Receive the question from JSON-RPC params and log it."""
        try:
            q = tools.ustr(question or "").strip()

            _logger.info(
                "AI Chat question: %r | user_id=%s",
                q,
                request.env.user.id if request.env.user else None,
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
