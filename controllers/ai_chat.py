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
    """AI Chat controller for website_ai_chat_min."""

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_aic_user_for_current_user(self):
        """
        Return the aic.user record for the current request user, or None.
        """
        user = request.env.user if request and request.env else None
        try:
            if not user or not getattr(user, "id", False):
                return None

            if getattr(user, "_name", "") != "res.users":
                return None

            aic_user_rec = (
                request.env["aic.user"]
                .sudo()
                .search(
                    [
                        ("aic_user_id", "=", user.id),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while looking up aic.user for user_id=%s: %s",
                getattr(user, "id", None),
                exc,
            )
            return None
        else:
            return aic_user_rec or None

    def _get_ai_config(self):
        """
        Return API key and File Store ID as saved via res.config.settings.

        Assumes fields are stored in ir.config_parameter as:
          - website_ai_chat_min.ai_api_key
          - website_ai_chat_min.file_store_id
        """
        try:
            icp = request.env["ir.config_parameter"].sudo()
            api_key = (icp.get_param("website_ai_chat_min.ai_api_key") or "").strip()
            file_store_id = (icp.get_param("website_ai_chat_min.file_store_id") or "").strip()
        except Exception as exc:
            _logger.exception("AI Chat: error while reading AI config: %s", exc)
            return {
                "api_key": "",
                "file_store_id": "",
            }
        else:
            return {
                "api_key": api_key,
                "file_store_id": file_store_id,
            }

    def _get_user_model_limits_for_current_user(self, model_name=None):
        """
        Return per-model limits for the current user.

        One user can have multiple lines (one per Gemini model).
        This helper:
          * Always returns a summary of ALL configured models
            (for logging / debugging).
          * Returns the limits for a specific model if `model_name` is set,
            using aic.user.get_user_model_limits().
          * If `model_name` is empty, falls back to the first active line.

        :param model_name: Gemini model string chosen on the UI
        :return: dict with keys:
            - model_name
            - prompt_limit
            - tokens_per_prompt
            - all_models: list of dicts
        """
        result = {
            "model_name": None,
            "prompt_limit": None,
            "tokens_per_prompt": None,
            "all_models": [],
        }

        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return result

        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            return result

        # Collect all model limits for this user (for logging)
        all_models = []
        try:
            for line in aic_user_rec.aic_line_ids.filtered(lambda l: l.active):
                try:
                    code = (
                        line.aic_model_id.aic_gemini_model
                        if line.aic_model_id
                        else None
                    )
                except Exception:
                    code = None

                all_models.append(
                    {
                        "model_name": code,
                        "prompt_limit": line.aic_prompt_limit,
                        "tokens_per_prompt": line.aic_tokens_per_prompt,
                    }
                )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error collecting model limits for user_id=%s: %s",
                getattr(user, "id", None),
                exc,
            )

        result["all_models"] = all_models

        # Normalise model_name coming from the UI
        gemini_model = tools.ustr(model_name or "").strip()

        # If UI selected a specific model, use aic.user.get_user_model_limits()
        if gemini_model:
            limits = None
            try:
                limits = (
                    request.env["aic.user"]
                    .sudo()
                    .get_user_model_limits(user, gemini_model)
                )
            except Exception as exc:
                _logger.exception(
                    "AI Chat: error reading model limits for user_id=%s, "
                    "model=%s: %s",
                    getattr(user, "id", None),
                    gemini_model,
                    exc,
                )
            else:
                if limits:
                    result["model_name"] = gemini_model
                    result["prompt_limit"] = limits.get("prompt_limit")
                    result["tokens_per_prompt"] = limits.get("tokens_per_prompt")
            return result

        # No model_name was passed: fallback = first active line
        try:
            line = aic_user_rec.aic_line_ids.filtered(lambda l: l.active)[:1]
        except Exception as exc:
            _logger.exception(
                "AI Chat: error determining default model for user_id=%s: %s",
                getattr(user, "id", None),
                exc,
            )
            return result

        if line:
            try:
                result["model_name"] = (
                    line.aic_model_id.aic_gemini_model
                    if line.aic_model_id
                    else None
                )
            except Exception:
                result["model_name"] = None

            result["prompt_limit"] = line.aic_prompt_limit
            result["tokens_per_prompt"] = line.aic_tokens_per_prompt

        return result

    # -------------------------------------------------------------------------
    # Routes
    # -------------------------------------------------------------------------

    @http.route(
        "/ai_chat/can_load",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def can_load(self, **kwargs):
        """
        Simple hook the JS widget calls to decide if it should mount.

        For now: always return show=True. You can later restrict this
        to only users that have an aic.user record.
        """
        return {"show": True}

    @http.route(
        "/ai_chat/models",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def get_models(self, **kwargs):
        """
        Return list of Gemini models and limits for the current user.

        Shape returned to JS:
        {
            "ok": True,
            "models": [
                {"model_name": "gemini-2.0-flash", "prompt_limit": 20, "tokens_per_prompt": 8192},
                ...
            ],
            "default_model": "gemini-2.0-flash"
        }
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {"ok": False, "models": []}

        limits_info = self._get_user_model_limits_for_current_user()
        models = limits_info.get("all_models") or []

        return {
            "ok": True,
            "models": models,
            "default_model": limits_info.get("model_name"),
        }


    @http.route(
        "/ai_chat/send",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def send(self, question=None, model_name=None, **kwargs):
        """
        Receive the question from JSON-RPC params and log it.

        Frontend may send the selected Gemini model as one of:
          - model_name
          - gemini_model
          - model
        """
        try:
            q = tools.ustr(question or "").strip()
            user = request.env.user if request and request.env else None

            # Which model did the UI ask for (if any)?
            selected_model = (
                model_name
                or kwargs.get("model_name")
                or kwargs.get("gemini_model")
                or kwargs.get("model")
            )

            # aic.user config for current user
            aic_user_rec = self._get_aic_user_for_current_user()
            has_aic_user = bool(aic_user_rec)

            # AI config (API key + File Store ID)
            ai_config = self._get_ai_config()
            api_key = ai_config.get("api_key") or ""
            masked_key = api_key[:6] + "..." if api_key else ""
            file_store_id = ai_config.get("file_store_id") or ""

            # Per-user / per-model limits (and full list for logging)
            limits_info = self._get_user_model_limits_for_current_user(
                model_name=selected_model,
            )

            all_models_summary = ", ".join(
                "{}: prompts={}, tokens={}".format(
                    item.get("model_name"),
                    item.get("prompt_limit"),
                    item.get("tokens_per_prompt"),
                )
                for item in limits_info.get("all_models") or []
            ) or "NO_MODEL_LIMITS"

            _logger.info(
                (
                    "AI Chat question: %r | user_id=%s | has_aic_user=%s | "
                    "aic_user_rec_id=%s | selected_model=%s | prompt_limit=%s | "
                    "tokens_per_prompt=%s | all_model_limits=[%s] | "
                    "file_store_id=%s | API_KEY=%s"
                ),
                q,
                getattr(user, "id", None),
                has_aic_user,
                aic_user_rec.id if aic_user_rec else None,
                limits_info.get("model_name"),
                limits_info.get("prompt_limit"),
                limits_info.get("tokens_per_prompt"),
                all_models_summary,
                file_store_id,
                masked_key,
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
