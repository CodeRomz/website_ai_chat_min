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

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# google-genai (Gemini) is optional at import time – we guard usage in _call_gemini.
try:
    import google.genai as genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover - library may not be installed on all envs
    genai = None
    genai_types = None


class AiChatController(http.Controller):
    """AI Chat controller for website_ai_chat_min.

    This controller exposes three JSON endpoints used by the frontend widget:

      * /ai_chat/can_load  – check if the current user is allowed to use AI chat.
      * /ai_chat/models    – list the Gemini models configured for the user.
      * /ai_chat/send      – send a prompt to Gemini using the selected model.
    """

    # -------------------------------------------------------------------------
    # Internal helpers – user / config
    # -------------------------------------------------------------------------

    def _get_aic_user_for_current_user(self):
        """Return the ``aic.user`` record for the current user, or ``None``.

        Access to the chat is governed by the presence of an *active* ``aic.user``
        record for the logged-in ``res.users`` record. Security groups control
        visibility of backend menus but the *runtime* gate is this record.
        """
        user = request.env.user if request and request.env else None
        try:
            if not user or not getattr(user, "id", False):
                return None

            AicUser = request.env["aic.user"].sudo()
            aic_user_rec = AicUser.search(
                [("aic_user_id", "=", user.id), ("active", "=", True)],
                limit=1,
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while fetching aic.user for user %s: %s",
                getattr(user, "id", None),
                exc,
            )
            return None
        else:
            return aic_user_rec or None

    def _get_ai_config(self):
        """Return API key and File Search store name from ``ir.config_parameter``.

        Expected keys in ``ir.config_parameter``::

            website_ai_chat_min.ai_api_key
            website_ai_chat_min.file_store_id   (File Search store name)

        The values are *not* validated here; that is left to ``_call_gemini``.
        """
        api_key = ""
        file_store_id = ""
        try:
            icp = request.env["ir.config_parameter"].sudo()
            api_key = (icp.get_param("website_ai_chat_min.ai_api_key") or "").strip()
            file_store_id = (
                icp.get_param("website_ai_chat_min.file_store_id") or ""
            ).strip()
        except Exception as exc:
            _logger.exception("AI Chat: error while reading AI config: %s", exc)
        finally:
            return {
                "api_key": api_key,
                "file_store_id": file_store_id,
            }

    # -------------------------------------------------------------------------
    # Internal helpers – per-user models & limits
    # -------------------------------------------------------------------------

    def _build_all_models_for_user(self, aic_user_rec):
        """Return a list of all active models configured for ``aic.user``.

        Each item in the returned list has the shape::

            {
                "model_name": "gemini-2.5-flash",
                "prompt_limit": 20,
                "tokens_per_prompt": 8192,
            }

        This is used exclusively by ``/ai_chat/models`` to render the chips on
        the frontend. No list of models is ever sent to Gemini.
        """
        models_list = []
        if not aic_user_rec:
            return models_list

        try:
            lines = aic_user_rec.sudo().aic_line_ids.filtered(lambda l: l.active)
            for line in lines:
                try:
                    code = (
                        line.aic_model_id.aic_gemini_model
                        if line.aic_model_id
                        else None
                    )
                except Exception:
                    code = None

                code = tools.ustr(code or "").strip()
                if not code:
                    continue

                models_list.append(
                    {
                        "model_name": code,
                        "prompt_limit": line.aic_prompt_limit,
                        "tokens_per_prompt": line.aic_tokens_per_prompt,
                    }
                )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while building model list for aic.user %s: %s",
                getattr(aic_user_rec, "id", None),
                exc,
            )

        return models_list

    def _resolve_model_limits_for_user(self, aic_user_rec, model_name=None):
        """Resolve the effective Gemini model and limits for this user.

        This method never builds ``all_models`` – it only returns the limits
        for *one* model (the one requested or the default).

        :param aic_user_rec: ``aic.user`` record for the current user.
        :param model_name: optional Gemini model code chosen in the UI.
        :return: dict with keys:

            {
                "requested_model_name": <raw string from UI or None>,
                "model_name":          <effective Gemini model or None>,
                "prompt_limit":        <int or None>,
                "tokens_per_prompt":   <int or None>,
            }
        """
        result = {
            "requested_model_name": tools.ustr(model_name or "").strip() or None,
            "model_name": None,
            "prompt_limit": None,
            "tokens_per_prompt": None,
        }

        if not aic_user_rec:
            return result

        user = aic_user_rec.sudo().aic_user_id
        requested = result["requested_model_name"]

        try:
            effective_model = None
            prompt_limit = None
            tokens_per_prompt = None

            # If the UI specified a model, try to resolve it via the helper on aic.user
            if requested:
                try:
                    limits = aic_user_rec.sudo().get_user_model_limits(user, requested)
                except Exception as exc:
                    _logger.exception(
                        "AI Chat: error in get_user_model_limits for user %s "
                        "and model %s: %s",
                        getattr(user, "id", None),
                        requested,
                        exc,
                    )
                    limits = None

                if limits:
                    effective_model = requested
                    prompt_limit = limits.get("prompt_limit")
                    tokens_per_prompt = limits.get("tokens_per_prompt")

            # If no model was requested or resolution failed, fallback to first line
            if not effective_model:
                line = (
                    aic_user_rec.sudo()
                    .aic_line_ids.filtered(lambda l: l.active)[:1]
                )
                if line:
                    line = line[0]
                    if line.aic_model_id and line.aic_model_id.aic_gemini_model:
                        effective_model = tools.ustr(
                            line.aic_model_id.aic_gemini_model
                        ).strip() or None
                        prompt_limit = line.aic_prompt_limit
                        tokens_per_prompt = line.aic_tokens_per_prompt

            result.update(
                {
                    "model_name": effective_model,
                    "prompt_limit": prompt_limit,
                    "tokens_per_prompt": tokens_per_prompt,
                }
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while resolving model limits for aic.user %s: %s",
                getattr(aic_user_rec, "id", None),
                exc,
            )

        return result

    # -------------------------------------------------------------------------
    # Internal helpers – Gemini config (generation + safety)
    # -------------------------------------------------------------------------

    def _build_gemini_safety_settings(self, icp):
        """Build per-category SafetySetting list based on system parameters.

        Each category reads its own dropdown value. If it's 'sdk_default' or
        empty, we skip it and let the SDK's default apply.
        """
        if not genai_types:
            return None

        settings = []
        categories = {
            "HARM_CATEGORY_HARASSMENT": "website_ai_chat_min.gemini_safety_harassment",
            "HARM_CATEGORY_HATE_SPEECH": "website_ai_chat_min.gemini_safety_hate",
            "HARM_CATEGORY_SEXUAL_CONTENT": "website_ai_chat_min.gemini_safety_sexual",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "website_ai_chat_min.gemini_safety_dangerous",
        }

        for category, param_key in categories.items():
            try:
                threshold = (icp.get_param(param_key) or "").strip()
            except Exception as exc:
                _logger.exception(
                    "AI Chat: error while reading safety parameter %s: %s",
                    param_key,
                    exc,
                )
                continue

            if not threshold or threshold == "sdk_default":
                # Let SDK defaults handle this category
                continue

            try:
                settings.append(
                    genai_types.SafetySetting(
                        category=category,
                        threshold=threshold,
                    )
                )
            except Exception as exc:
                _logger.warning(
                    "AI Chat: invalid Gemini SafetySetting for %s (threshold=%s): %s",
                    category,
                    threshold,
                    exc,
                )

        return settings or None

    def _build_gemini_generation_config(self, max_output_tokens, tools_param):
        """Build GenerateContentConfig from ir.config_parameter + per-user limits."""
        if not genai_types:
            raise UserError(
                _(
                    "Google Generative AI Python client is not installed. "
                    "Please contact your administrator."
                )
            )

        icp = request.env["ir.config_parameter"].sudo()

        def _float_param(key, default):
            value = icp.get_param(key)
            try:
                return float(value) if value is not None else default
            except (TypeError, ValueError):
                _logger.warning(
                    "AI Chat: invalid float config %s=%s, using default %s",
                    key,
                    value,
                    default,
                )
                return default

        def _int_param(key, default):
            value = icp.get_param(key)
            try:
                return int(value) if value is not None else default
            except (TypeError, ValueError):
                _logger.warning(
                    "AI Chat: invalid int config %s=%s, using default %s",
                    key,
                    value,
                    default,
                )
                return default

        temperature = _float_param("website_ai_chat_min.gemini_temperature", 0.2)
        top_p = _float_param("website_ai_chat_min.gemini_top_p", 0.95)
        top_k = _int_param("website_ai_chat_min.gemini_top_k", 40)
        candidate_count = _int_param("website_ai_chat_min.gemini_candidate_count", 1)
        safety_settings = self._build_gemini_safety_settings(icp)

        # NEW: global system instruction for Gemini (persona / behaviour / constraints)
        system_instruction = (
                icp.get_param("website_ai_chat_min.gemini_system_instruction") or ""
        ).strip()

        try:
            generation_config = genai_types.GenerateContentConfig(
                # Only pass system_instruction if non-empty; otherwise let SDK defaults apply.
                system_instruction=system_instruction or None,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                candidate_count=candidate_count,
                max_output_tokens=max_output_tokens,
                tools=tools_param,
                safety_settings=safety_settings,
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while building Gemini generation config: %s", exc
            )
            # Let caller wrap this in a user-facing error
            raise
        else:
            return generation_config

    # -------------------------------------------------------------------------
    # Internal helpers – Gemini call
    # -------------------------------------------------------------------------

    def _call_gemini(self, api_key, file_store_id, model_name, prompt, max_output_tokens):
        """Call Google Generative AI (Gemini) with optional File Search tool.

        :param api_key:          Gemini API key
        :param file_store_id:    File Search store name (fileSearchStores/...)
        :param model_name:       Gemini model identifier (from ``aic.user`` line)
        :param prompt:           user question (string)
        :param max_output_tokens: per-prompt token cap (from ``aic.user`` line)
        :return: reply text (string)
        :raises UserError: on configuration or runtime errors.
        """
        if not genai or not genai_types:
            raise UserError(
                _(
                    "Google Generative AI Python client is not installed. "
                    "Please contact your administrator."
                )
            )

        api_key = tools.ustr(api_key or "").strip()
        model_name = tools.ustr(model_name or "").strip()
        if not api_key or not model_name:
            raise UserError(
                _(
                    "AI backend is not fully configured. "
                    "Please contact your administrator."
                )
            )

        # Respect per-model token limit from aic.user
        try:
            max_tokens = int(max_output_tokens) if max_output_tokens else 512
        except (TypeError, ValueError):
            max_tokens = 512

        # Configure File Search tool if a store is specified
        tools_param = None
        if file_store_id:
            try:
                tools_param = [
                    genai_types.Tool(
                        file_search=genai_types.FileSearch(
                            file_search_store_names=[tools.ustr(file_store_id).strip()]
                        )
                    )
                ]
            except Exception as exc:
                _logger.exception(
                    "AI Chat: error while configuring File Search tool: %s", exc
                )
                tools_param = None

        try:
            generation_config = self._build_gemini_generation_config(
                max_output_tokens=max_tokens,
                tools_param=tools_param,
            )

            client = genai.Client(api_key=api_key)

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=generation_config,
            )
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while calling Gemini model %s: %s", model_name, exc
            )
            raise UserError(
                _("Error while calling the AI model. Please try again later.")
            )

        # Extract plain text reply from the first candidate
        try:
            if not response or not getattr(response, "candidates", None):
                return ""

            candidate = response.candidates[0]
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            texts = []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    texts.append(tools.ustr(text))

            return "\n".join(texts).strip()
        except Exception as exc:
            _logger.exception(
                "AI Chat: error while processing Gemini response: %s", exc
            )
            raise UserError(
                _(
                    "Error while processing the AI response. "
                    "Please contact your administrator."
                )
            )

    # -------------------------------------------------------------------------
    # Public JSON routes
    # -------------------------------------------------------------------------

    @http.route(
        "/ai_chat/can_load",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def can_load(self, **kwargs):
        """Return ``{\"show\": True/False}`` for the frontend widget.

        The widget is only mounted if an active ``aic.user`` record exists for
        the current logged-in user.
        """
        try:
            aic_user_rec = self._get_aic_user_for_current_user()
            return {"show": bool(aic_user_rec)}
        except Exception as exc:
            _logger.exception("AI Chat: error in /ai_chat/can_load: %s", exc)
            # Fail closed – better to hide the widget than to leak errors.
            return {"show": False}

    @http.route(
        "/ai_chat/models",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def get_models(self, **kwargs):
        """Return the list of models and the default model for the current user.

        Response shape (JSON-RPC ``result`` payload)::

            {
                "ok": true/false,
                "models": [
                    {
                        "model_name": "gemini-2.5-flash",
                        "prompt_limit": 20,
                        "tokens_per_prompt": 8192,
                    },
                    ...
                ],
                "default_model": "gemini-2.5-flash" | null,
            }
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {"ok": False, "models": [], "default_model": None}

        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            return {"ok": False, "models": [], "default_model": None}

        models_list = self._build_all_models_for_user(aic_user_rec)
        limits_info = self._resolve_model_limits_for_user(aic_user_rec, None)
        default_model = limits_info.get("model_name")

        if not default_model and models_list:
            default_model = models_list[0].get("model_name")

        return {
            "ok": True,
            "models": models_list,
            "default_model": default_model,
        }

    @http.route(
        "/ai_chat/send",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def send(self, question=None, model_name=None, **kwargs):
        """Receive the question from JSON-RPC and forward it to Gemini.

        Frontend may send the selected Gemini model using any of these keys:

          * ``model_name``
          * ``gemini_model``
          * ``model``

        Flow:

          * Use the model selected in the frontend.
          * Validate against ``aic.user`` via ``get_user_model_limits()``.
          * Use ``tokens_per_prompt`` as ``max_output_tokens``.
          * Use File Search if ``file_store_id`` is configured.
        """
        user = request.env.user if request and request.env else None
        if not user or not getattr(user, "id", False):
            return {
                "ok": False,
                "reply": _("You must be logged in to use AI chat."),
            }

        # Normalise the question
        q = tools.ustr(question or kwargs.get("question") or "").strip()
        if not q:
            return {"ok": False, "reply": _("Please enter a message.")}

        # Authorisation: must have an active aic.user record
        aic_user_rec = self._get_aic_user_for_current_user()
        if not aic_user_rec:
            return {
                "ok": False,
                "reply": _("You are not allowed to use AI chat."),
            }

        # Configuration
        cfg = self._get_ai_config()
        api_key = cfg.get("api_key") or ""
        file_store_id = cfg.get("file_store_id") or ""
        if not tools.ustr(api_key).strip():
            _logger.warning(
                "AI Chat: Gemini API key is not configured (website_ai_chat_min.ai_api_key)."
            )
            return {
                "ok": False,
                "reply": _(
                    "AI backend is not configured. Please contact your administrator."
                ),
            }

        # Model coming from the frontend (single choice)
        selected_model_raw = tools.ustr(
            model_name
            or kwargs.get("model_name")
            or kwargs.get("gemini_model")
            or kwargs.get("model")
            or ""
        ).strip() or None

        limits_info = self._resolve_model_limits_for_user(
            aic_user_rec, selected_model_raw
        )
        effective_model = limits_info.get("model_name")
        max_output_tokens = limits_info.get("tokens_per_prompt")

        if not effective_model:
            _logger.warning(
                "AI Chat: user %s requested unsupported model %s.",
                getattr(user, "id", None),
                selected_model_raw,
            )
            return {
                "ok": False,
                "reply": _("The selected model is not configured for your user."),
            }

        try:
            reply_text = self._call_gemini(
                api_key=api_key,
                file_store_id=file_store_id,
                model_name=effective_model,
                prompt=q,
                max_output_tokens=max_output_tokens,
            )
        except UserError as ue:
            # Extract a clean message for the frontend
            msg = tools.ustr(getattr(ue, "name", None) or "").strip()
            if not msg and ue.args:
                msg = tools.ustr(ue.args[0])
            if not msg:
                msg = _("AI backend error.")
            return {
                "ok": False,
                "reply": msg,
            }
        except Exception as exc:  # pragma: no cover - safety net
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
