{
    "name": "Website AI Chat (Minimal)",
    "description": """
        Website AI Chat (Minimal) for Odoo 17.0 CE:
        - Standalone page (auth='user')
        - Session-only history, no DB persistence (GDPR-friendly)
        - Google Gemini backends
        - Robust CSRF, group gating, safe DOM rendering
            """,
    "summary": "Minimal AI chat for Website with OpenAI/Gemini, GDPR-friendly (Odoo 17 CE).",
    "version": "17.0.1.2.0",
    "category": "Website",
    "license": "LGPL-3",
    "author": "Romualdo Jr",
    "website": "https://github.com/CodeRomz",
    "application": True,
    "installable": True,
    "depends": ["website"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/ai_chat_templates.xml",
        "views/aic_main_menu.xml",
        "views/aic_admin.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "website_ai_chat_min/static/src/css/ai_chat.css",
            "website_ai_chat_min/static/src/js/ai_chat.js",
        ],
    },
    "external_dependencies": {
        "python": ["google-genai"]
    },
}
