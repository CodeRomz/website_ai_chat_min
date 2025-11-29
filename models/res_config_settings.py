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


    # ---------------------------------------------------------------------
    # Gemini Generation Behaviour (GenerateContentConfig)
    # ---------------------------------------------------------------------
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
        help="Number of candidate completions to generate. 1 is recommended for production.",
    )

    # ---------------------------------------------------------------------
    # Per-category Gemini Safety Thresholds (one dropdown per category)
    # ---------------------------------------------------------------------
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
