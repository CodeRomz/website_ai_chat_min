# -*- coding: utf-8 -*-
{
    "name": "Website AI Chat (Minimal)",
    "summary": "Minimal AI chat bubble for Website with OpenAI/Gemini backends, PDF grounding, and GDPR-friendly defaults.",
    "version": "17.0.1.0.0",
    "category": "Website",
    "license": "LGPL-3",
    "author": "Your Company",
    "website": "https://example.com",
    "application": False,
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
        ]
    },
    "external_dependencies": {
        "python": ["openai", "google-generativeai", "pypdf"]
    },
    "description": """
Minimal AI chat bubble for Odoo 17 CE Website. 
 - Standalone /ai-chat page
 - Per-user session, no persistence (GDPR-friendly)
 - Supports OpenAI & Google Gemini
 - Optional PDF grounding (server folder)
 - Robust CSRF, group checks, safe DOM text rendering
    """,
}
