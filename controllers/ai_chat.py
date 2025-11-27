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

import time
from typing import Any, Dict


class AiChatController(http.Controller):
    """Minimal AI chat controller.

    Step 1: only capture user question and log it in the backend.
    No AI / Gemini / quota logic yet.
    """

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _extract_question(self) -> str:
        """Extract the `question` string from the JSON-RPC style payload."""
        data: Dict[str, Any] = request.jsonrequest or {}
        params: Dict[str, Any] = data.get("params") or {}
        question = (params.get("question") or "").strip()
        return question

    def _log_question(self, question: str) -> None:
        """Log question with basic context (user, partner, timestamp)."""
        try:
            user = request.env.user
            partner = user.partner_id if user else None
            _logger.info(
                "AI Chat question logged | user_id=%s name=%s partner_id=%s "
                "question=%r timestamp=%s",
                user.id if user else None,
                user.name if user else "Public",
                partner.id if partner else None,
                question,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            # We don't want logging failure to crash the route
            _logger.exception("AI Chat: failed to log question")

    # -------------------------------------------------------------------------
    # Routes
    # -------------------------------------------------------------------------
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
        """Receive the question, log it, and return a simple echo reply."""
        try:
            question = self._extract_question()

            if not question:
                return {
                    "ok": False,
                    "reply": _("Please enter a message."),
                }

            # Log question in backend logs
            self._log_question(question)

        except Exception as exc:
            _logger.exception("AI Chat: error while handling request: %s", exc)
            return {
                "ok": False,
                "reply": _(
                    "Something went wrong while logging your message. "
                    "Please try again later."
                ),
            }
