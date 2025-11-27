# -*- coding: utf-8 -*-
from __future__ import annotations

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
    """Minimal AI chat controller.

    Step 1: only capture user question and log it.
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
        """Tell the frontend whether to show the widget.

        For now: always allow. Later we can plug in aic.admin checks here.
        """
        return {"show": True}

    @http.route(
        "/ai_chat/send",
        type="json",
        auth="public",
        methods=["POST"],
        csrf=True,
    )
    def send(self, **kwargs):
        """Receive the question, log it, and return a simple reply."""
        try:
            data = request.jsonrequest or {}
            params = data.get("params") or {}
            question = tools.ustr(params.get("question") or "").strip()

            # Direct logging, no helper, no extra formatting
            _logger.info(
                "AI Chat question: %r | user_id=%s",
                question,
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
