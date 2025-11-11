{
    "name": "Website AI Chat (Minimal)",
    "summary": "Minimal AI chat for Website with OpenAI/Gemini, PDF grounding, GDPR-friendly (Odoo 17 CE).",
    "version": "17.0.1.2.0",
    "category": "Website",
    "license": "LGPL-3",
    "author": "Romualdo Jr",
    "website": "https://github.com/CodeRomz",
    "application": True,
    "installable": True,
    "depends": ["base", "web", "website"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/ai_chat_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "website_ai_chat_min/static/src/css/ai_chat.css",
            "website_ai_chat_min/static/src/js/ai_chat.js",
        ],
    },
    "external_dependencies": {
        "python": ["openai", "google-genai"]
    },
    "description": """
Website AI Chat (Minimal) for Odoo 17.0 CE:
- Standalone page (auth='user')
- Session-only history, no DB persistence (GDPR-friendly)
- Google Gemini backends
- Robust CSRF, group gating, safe DOM rendering
""",
}
