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
      - log whether the current user has an aic.user configuration.
      - log model name, prompt limit, tokens per prompt for the current user.
    """

    def _get_aic_user_for_current_user(self):
        """
        Return the aic.user record for the current request user, or None.

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
            return admin_rec or None

        except Exception as exc:
            _logger.exception(
                "AI Chat: error while looking up aic.user for user_id=%s: %s",
                getattr(user, "id", None),
                exc,
            )
            return None

    def _get_ai_config(self):
        """
        Return API key and File Store ID as saved via res.config.settings.

        Values are stored in ir.config_parameter via config_parameter on fields:
          - website_ai_chat_min.ai_api_key
          - website_ai_chat_min.file_store_id
        """
        try:
            icp = request.env["ir.config_parameter"].sudo()

            api_key = (icp.get_param("website_ai_chat_min.ai_api_key") or "").strip()
            file_store_id = (
                icp.get_param("website_ai_chat_min.file_store_id") or ""
            ).strip()

            # NOTE: we do NOT log the raw API key here, only masked in send()
            return {
                "api_key": api_key,
                "file_store_id": file_store_id,
            }

        except Exception as exc:
            _logger.exception("AI Chat: error while reading AI config: %s", exc)
            return {
                "api_key": "",
                "file_store_id": "",
            }

    def _get_user_model_limits_for_current_user(self, model_name=None):
        """
        Return model limits for the current user and the given Gemini model.

        Uses aic.user.get_user_model_limits() which already encapsulates
        the logic for (user, model) lookup.

        :param model_name: Gemini model string (e.g. 'gemini-2.0-flash-lite')
                           or None. If None/empty, limits will be None.
        :return: dict with keys:
                 - model_name
                 - prompt_limit
                 - tokens_per_prompt
                 Values are None when not configured.
        """
        user = request.env.user if request and request.env else None
        gemini_model = tools.ustr(model_name or "").strip()

        result = {
            "model_name": gemini_model or None,
            "prompt_limit": None,
            "tokens_per_prompt": None,
        }

        # Quick guards before touching aic.user
        if not user or not getattr(user, "id", False):
            return result

        if getattr(user, "_name", "") != "res.users":
            return result

        if not gemini_model:
            # No model requested by the frontend
            return result

        limits = None
        try:
            limits = (request.env["aic.user"].sudo().get_user_model_limits(user, gemini_model))
        except Exception as exc:
            _logger.exception(
                "AI Chat: error reading model limits for user_id=%s, model=%s: %s",
                getattr(user, "id", None),
                gemini_model,
                exc,
            )
        else:
            if limits:
                # aic.user.get_user_model_limits returns:
                #   {'prompt_limit': int, 'tokens_per_prompt': int}
                result["prompt_limit"] = limits.get("prompt_limit")
                result["tokens_per_prompt"] = limits.get("tokens_per_prompt")
        finally:
            return result

    @http.route("/ai_chat/can_load", type="json", auth="user", methods=["POST"], csrf=True, )
    def can_load(self, **kwargs):
        # For now: always allow mounting. We'll plug aic.user checks here later.
        return {"show": True}

    @http.route("/ai_chat/send", type="json", auth="user", methods=["POST"], csrf=True)
    def send(self, question=None, model_name=None, **kwargs):
        """
        Receive the question from JSON-RPC params, look up model limits,
        and log everything.

        Frontend is expected to pass the selected Gemini model as either:
          - model_name
          - gemini_model
        """
        try:
            q = tools.ustr(question or "").strip()
            user = request.env.user if request and request.env else None

            # Model name from payload (we accept two possible keys)
            model_name = (
                model_name
                or kwargs.get("model_name")
                or kwargs.get("gemini_model")
            )

            # Check if this user has an aic.user config
            admin_rec = self._get_aic_user_for_current_user()
            is_ai_user = bool(admin_rec)

            # Load AI config (api key + file store id)
            ai_config = self._get_ai_config()

            # Read per-user/per-model limits
            limits_info = self._get_user_model_limits_for_current_user( model_name=model_name )

            # Mask API key in logs so we don't leak the secret
            api_key = ai_config.get("api_key") or ""
            masked_key = api_key[:6] + "..." if api_key else ""

            _logger.info(
                (
                    "AI Chat question: %r | user_id=%s | has_aic_user=%s | "
                    "aic_user_id=%s | model_name=%s | prompt_limit=%s | "
                    "tokens_per_prompt=%s | file_store_id=%s | API_KEY=%s"
                ),
                q,
                getattr(user, "id", None),
                is_ai_user,
                admin_rec.id if admin_rec else None,
                limits_info.get("model_name"),
                limits_info.get("prompt_limit"),
                limits_info.get("tokens_per_prompt"),
                ai_config.get("file_store_id"),
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
