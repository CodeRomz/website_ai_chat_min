from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning, AccessDenied, AccessError, CacheMiss, MissingError
import logging

_logger = logging.getLogger(__name__)


# Central list of Gemini safety threshold options, reused by all safety fields.
GEMINI_SAFETY_SELECTION = [
    ("sdk_default", "SDK default"),
    ("BLOCK_NONE", "Block none"),
    ("BLOCK_ONLY_HIGH", "Block only high"),
    ("BLOCK_MEDIUM_AND_ABOVE", "Block medium and above"),
    ("BLOCK_LOW_AND_ABOVE", "Block low and above"),
]


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -------------------------------------------------------------------------
    # Default Gemini system instruction / persona
    # -------------------------------------------------------------------------
    aic_gemini_system_instruction_id = fields.Many2one(
        comodel_name="aic.gemini_system_instruction",
        string="Default Gemini system instruction",
        help=(
            "Optional default system instruction/persona used by the website AI "
            "chat. This will be sent as system_instruction in the "
            "GenerateContentConfig for Gemini."
        ),
    )

    # -------------------------------------------------------------------------
    # Gemini Generation Behaviour (GenerateContentConfig)
    # -------------------------------------------------------------------------
    aic_gemini_temperature = fields.Float(
        string="Gemini Temperature",
        default=0.2,
        config_parameter="website_ai_chat_min.gemini_temperature",
        help=(
            "Controls randomness. 0 = deterministic, 1 = very creative.\n"
            "Typical range: 0.1 – 0.7. Default matches the current hard-coded value (0.2)."
        ),
    )

    aic_gemini_top_p = fields.Float(
        string="Gemini Top P",
        default=0.95,
        config_parameter="website_ai_chat_min.gemini_top_p",
        help=(
            "Nucleus sampling (top-p) – cumulative probability mass to sample from.\n"
            "Typical range: 0.8 – 0.95."
        ),
    )

    aic_gemini_top_k = fields.Integer(
        string="Gemini Top K",
        default=40,
        config_parameter="website_ai_chat_min.gemini_top_k",
        help=(
            "Limits how many candidate tokens are considered at each step.\n"
            "Use 0 to let the model decide automatically."
        ),
    )

    aic_gemini_candidate_count = fields.Integer(
        string="Gemini Candidate Count",
        default=1,
        config_parameter="website_ai_chat_min.gemini_candidate_count",
        help=(
            "Number of candidate completions to generate.\n"
            "1 is recommended for production; higher values increase latency and quota usage."
        ),
    )

    # -------------------------------------------------------------------------
    # Per-category Gemini Safety Thresholds (one dropdown per category)
    # -------------------------------------------------------------------------
    aic_gemini_safety_harassment = fields.Selection(
        selection=GEMINI_SAFETY_SELECTION,
        string="Harassment safety threshold",
        default="sdk_default",
        config_parameter="website_ai_chat_min.gemini_safety_harassment",
        help="Safety threshold for harassment-related content.",
    )

    aic_gemini_safety_hate = fields.Selection(
        selection=GEMINI_SAFETY_SELECTION,
        string="Hate speech safety threshold",
        default="sdk_default",
        config_parameter="website_ai_chat_min.gemini_safety_hate",
        help="Safety threshold for hate-speech-related content.",
    )

    aic_gemini_safety_sexual = fields.Selection(
        selection=GEMINI_SAFETY_SELECTION,
        string="Sexual content safety threshold",
        default="sdk_default",
        config_parameter="website_ai_chat_min.gemini_safety_sexual",
        help="Safety threshold for sexual content.",
    )

    aic_gemini_safety_dangerous = fields.Selection(
        selection=GEMINI_SAFETY_SELECTION,
        string="Dangerous content safety threshold",
        default="sdk_default",
        config_parameter="website_ai_chat_min.gemini_safety_dangerous",
        help="Safety threshold for dangerous content.",
    )

    # -------------------------------------------------------------------------
    # Validation – keep params within sane/SDK-friendly ranges
    # -------------------------------------------------------------------------
    @api.constrains(
        "aic_gemini_temperature",
        "aic_gemini_top_p",
        "aic_gemini_top_k",
        "aic_gemini_candidate_count",
    )
    def _check_gemini_parameters(self):
        """
        Guardrails for misconfiguration in production:
        - temperature: [0.0, 2.0]
        - top_p:      (0.0, 1.0]
        - top_k:      >= 0
        - candidates: >= 1
        """
        for rec in self:
            # Temperature
            if rec.aic_gemini_temperature is not None and not (
                0.0 <= rec.aic_gemini_temperature <= 2.0
            ):
                raise ValidationError(
                    _(
                        "Gemini temperature must be between 0.0 and 2.0. "
                        "Current value: %(value)s",
                        value=rec.aic_gemini_temperature,
                    )
                )

            # Top P
            if rec.aic_gemini_top_p is not None and not (
                0.0 < rec.aic_gemini_top_p <= 1.0
            ):
                raise ValidationError(
                    _(
                        "Gemini Top P must be in the range (0.0, 1.0]. "
                        "Current value: %(value)s",
                        value=rec.aic_gemini_top_p,
                    )
                )

            # Top K
            if rec.aic_gemini_top_k is not None and rec.aic_gemini_top_k < 0:
                raise ValidationError(
                    _(
                        "Gemini Top K cannot be negative. "
                        "Current value: %(value)s",
                        value=rec.aic_gemini_top_k,
                    )
                )

            # Candidate count
            if (
                rec.aic_gemini_candidate_count is not None
                and rec.aic_gemini_candidate_count < 1
            ):
                raise ValidationError(
                    _(
                        "Gemini Candidate Count must be at least 1. "
                        "Current value: %(value)s",
                        value=rec.aic_gemini_candidate_count,
                    )
                )

    # -------------------------------------------------------------------------
    # Persist Many2one via ir.config_parameter
    # -------------------------------------------------------------------------
    @api.model
    def get_values(self):
        """
        Load the selected Gemini system instruction from ir.config_parameter,
        in addition to the standard config_parameter-backed fields.
        """
        res = super().get_values()
        icp_sudo = self.env["ir.config_parameter"].sudo()

        raw_instruction_id = icp_sudo.get_param(
            "website_ai_chat_min.gemini_system_instruction_id"
        )

        instruction_id = False
        if raw_instruction_id:
            try:
                instruction_id = int(raw_instruction_id)
            except (TypeError, ValueError):
                _logger.warning(
                    "Invalid gemini_system_instruction_id in config: %s; resetting.",
                    raw_instruction_id,
                )
                instruction_id = False

        res.update(
            aic_gemini_system_instruction_id=instruction_id,
        )
        return res

    def set_values(self):
        """
        Store the selected Gemini system instruction in ir.config_parameter.
        Other fields continue to use the built-in config_parameter mechanism.
        """
        super().set_values()
        icp_sudo = self.env["ir.config_parameter"].sudo()

        icp_sudo.set_param(
            "website_ai_chat_min.gemini_system_instruction_id",
            self.aic_gemini_system_instruction_id.id or False,
        )
