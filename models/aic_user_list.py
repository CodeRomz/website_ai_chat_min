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

# Optional dependency: google-genai
try:
    from google import genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover - optional dependency
    genai = None
    genai_types = None


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
            file_store_id = (
                icp.get_param("website_ai_chat_min.file_store_id") or ""
            ).strip()
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
            (for feeding the frontend model chips).
          * Returns the limits for a specific model if `model_name` is set,
            using aic.user.get_user_model_limits().
          * If `model_name` is empty, falls back to the first active line.

        :param model_name: Gemini model string chosen on the UI
        :return: dict with keys:
            - requested_model_name  (raw from UI, normalized)
            - model_name            (effective, allowed model)
            - prompt_limit
            - tokens_per_prompt
            - all_models: list of dicts
        """
        requested_model = tools.ustr(model_name or "").strip()

        result = {
            "requested_model_name": requested_model,
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

        # Collect all model limits for this user (for UI)
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

        # If UI selected a specific model, use aic.user.get_user_model_limits()
        gemini_model = requested_model
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
            # We do NOT auto-switch to another model if the requested one
            # is not configured; result["model_name"] stays None in that case.
            return result

        # No model_name passed at all: fallback = first active line
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

    def _call_gemini(
        self,
        api_key,
        file_store_id,
        model_name,
        prompt,
        max_output_tokens,
    ):
        """
        Call Google Generative AI (Gemini) with optional File Search tool.

        :param api_key: Google Generative AI API key
        :param file_store_id: ID used for File Search configuration
        :param model_name: Gemini model identifier (from aic.user line)
        :param prompt: user question (string)
        :param max_output_tokens: per-prompt token cap (from aic.user line)
        :return: reply text (string)
        """
        if not genai or not genai_types:
            raise UserError(
                _(
                    "Google Generative AI Python client is not installed. "
                    "Please contact your administrator."
                )
            )

        try:
            client = genai.Client(api_key=api_key)
        except Exception as exc:
            _logger.exception(
                "AI Chat: could not initialize Google Generative AI client: %s", exc
            )
            raise UserError(
                _("AI backend configuration error. Please contact your administrator.")
            )

        # Respect per-model token limit from aic.user
        try:
            max_tokens = int(max_output_tokens) if max_output_tokens else 512
        except (TypeError, ValueError):
            max_tokens = 512

        generation_config = genai_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=max_tokens,
        )

        # Attach File Search tool if a file_store_id is configured.
        # Exact mapping of file_store_id to corpora/file store can be refined
        # based on how you've provisioned File Search in Google Cloud.
        tools = None
        if file_store_id:
            try:
                tools = [
                    genai_types.Tool(
                        file_search=genai_types.FileSearch(
                            corpora=[
                                genai_types.Corpus(name=file_store_id),
                            ]
                        )
                    )
                ]
            except Exception as exc:
                # If we fail to prepare File Search, we fall back to pure text chat.
                _logger.exception(
                    "AI Chat: failed to prepare file search tool: %s", exc
                )
                tools = None

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=generation_config,
                tools=tools,
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while calling Google Generative AI model '%s': %s",
                model_name,
                exc,
            )
            raise UserError(
                _("Error while calling the AI model. Please try again later.")
            )

        if not response or not getattr(response, "candidates", None):
            return ""

        candidate = response.candidates[0]
        content = getattr(candidate, "content", None)

        text_chunks = []
        if content and getattr(content, "parts", None):
            for part in content.parts:
                text_val = getattr(part, "text", None)
                if text_val:
                    text_chunks.append(text_val)

        return "\n".join(text_chunks).strip()

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
        Hook the JS widget calls to decide if it should mount.

        Only users that have an aic.user record are allowed to use the chat.
        """
        aic_user_rec = self._get_aic_user_for_current_user()
        return {"show": bool(aic_user_rec)}

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
                {
                    "model_name": "gemini-2.0-flash",
                    "prompt_limit": 20,
                    "tokens_per_prompt": 8192
                },
                ...
            ],
            "default_model": "gemini-2.0-flash"
        }
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {"ok": False, "models": []}

        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            # User is logged in but not authorized via aic.user
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
        Receive the question from JSON-RPC params and forward it to Gemini.

        Frontend may send the selected Gemini model as one of:
          - model_name
          - gemini_model
          - model

        Steps:
          * Take the model selected in the frontend chips.
          * Look up that exact model in aic.user limits.
          * Use aic.user tokens_per_prompt as max_output_tokens.
          * Use File Search if file_store_id is configured.
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {
                "ok": False,
                "reply": _("You must be logged in to use AI chat."),
            }

        q = tools.ustr(question or "").strip()
        if not q:
            return {
                "ok": False,
                "reply": _("Please enter a message."),
            }

        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            return {
                "ok": False,
                "reply": _("You are not allowed to use AI chat."),
            }

        ai_config = self._get_ai_config()
        api_key = (ai_config.get("api_key") or "").strip()
        file_store_id = (ai_config.get("file_store_id") or "").strip()

        if not api_key:
            return {
                "ok": False,
                "reply": _(
                    "The AI backend is not configured. "
                    "Please contact your administrator."
                ),
            }

        # Which model did the UI ask for (if any)?
        selected_model_raw = tools.ustr(
            model_name
            or kwargs.get("model_name")
            or kwargs.get("gemini_model")
            or kwargs.get("model")
            or ""
        ).strip() or None

        limits_info = self._get_user_model_limits_for_current_user(
            model_name=selected_model_raw,
        )

        effective_model = limits_info.get("model_name")
        tokens_per_prompt = limits_info.get("tokens_per_prompt")

        if not effective_model:
            return {
                "ok": False,
                "reply": _(
                    "The selected model is not configured for your user. "
                    "Please choose another model."
                ),
            }

        try:
            reply_text = self._call_gemini(
                api_key=api_key,
                file_store_id=file_store_id,
                model_name=effective_model,
                prompt=q,
                max_output_tokens=tokens_per_prompt,
            )
        except UserError as ue:
            # Surface functional error messages to the user
            msg = tools.ustr(getattr(ue, "name", None) or "").strip()
            if not msg and ue.args:
                msg = tools.ustr(ue.args[0])
            if not msg:
                msg = _("AI backend error.")
            return {
                "ok": False,
                "reply": msg,
            }
        except Exception as exc:
            _logger.exception("AI Chat: unexpected error while calling Gemini: %s", exc)
            return {
                "ok": False,
                "reply": _("Unexpected error while calling the AI backend."),
            }

        if not reply_text:
            reply_text = _("No answer was returned by the AI model.")

        return {
            "ok": True,
            "reply": reply_text,
        }
