# -*- coding: utf-8 -*-

from odoo import http, models, fields, api, tools, _
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
        Return API key and File Search store name from ir.config_parameter.

        Expected keys in ir.config_parameter:
          - website_ai_chat_min.ai_api_key
          - website_ai_chat_min.file_store_id   (File Search store name)
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

        - Always returns a list of all models for the UI chips (all_models).
        - If model_name is provided, uses aic.user.get_user_model_limits(user, model).
        - If not, falls back to the first active line.

        :param model_name: Gemini model string chosen on the UI
        :return: dict:
            requested_model_name, model_name,
            prompt_limit, tokens_per_prompt,
            all_models (list of dicts)
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

        # Build list of all models for this user (for chips in frontend)
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

        # If UI requested a specific model, ask aic.user for its limits
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

            # Do not auto-fallback to another model here; keep strict.
            return result

        # No model_name supplied → use first active line as default
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

    def _call_gemini(self, api_key, file_store_id, model_name, prompt, max_output_tokens, ):
        """
        Call Google Generative AI (Gemini) with optional File Search tool.

        :param api_key: Gemini API key
        :param file_store_id: File Search store name (fileSearchStores/...)
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

        # Configure File Search tool if a store is specified
        tools = None
        if file_store_id:
            try:
                tools = [
                    genai_types.Tool(
                        file_search=genai_types.FileSearch(
                            file_search_store_names=[file_store_id]
                        )
                    )
                ]
            except Exception as exc:
                # If File Search wiring fails, fall back to plain chat
                _logger.exception(
                    "AI Chat: failed to prepare File Search tool: %s", exc
                )
                tools = None

        # Build generation config; tools live *inside* GenerateContentConfig
        config_kwargs = {
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
        if tools:
            config_kwargs["tools"] = tools

        generation_config = genai_types.GenerateContentConfig(**config_kwargs)

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=generation_config,
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

    @http.route("/ai_chat/can_load", type="json", auth="user", methods=["POST"], csrf=True, )
    def can_load(self, **kwargs):
        """
        JS checks this to know if it should mount the chat widget.

        Only users with an active aic.user record are allowed.
        """
        aic_user_rec = self._get_aic_user_for_current_user()
        return {"show": bool(aic_user_rec)}

    @http.route("/ai_chat/models", type="json", auth="user", methods=["POST"], csrf=True, )
    def get_models(self, **kwargs):
        """
        Return list of Gemini models and limits for the current user.

        Response shape:
        {
            "ok": True/False,
            "models": [
                {
                    "model_name": "gemini-2.5-flash",
                    "prompt_limit": 20,
                    "tokens_per_prompt": 8192
                },
                ...
            ],
            "default_model": "gemini-2.5-flash"
        }
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {"ok": False, "models": []}

        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            return {"ok": False, "models": []}

        limits_info = self._get_user_model_limits_for_current_user()
        models = limits_info.get("all_models") or []

        return {
            "ok": True,
            "models": models,
            "default_model": limits_info.get("model_name"),
        }

    @http.route("/ai_chat/send", type="json", auth="user", methods=["POST"], csrf=True, )
    def send(self, question=None, model_name=None, **kwargs):
        """
        Receive the question from JSON-RPC and forward it to Gemini.

        Frontend may send the selected Gemini model as:
          - model_name
          - gemini_model
          - model

        Flow:
          * Use the model selected in the frontend.
          * Validate against aic.user via get_user_model_limits().
          * Use tokens_per_prompt as max_output_tokens.
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
