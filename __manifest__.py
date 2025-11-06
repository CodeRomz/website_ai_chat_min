# -*- coding: utf-8 -*-
{
    "name": "Website AI Chat (Minimal)",
    "summary": "AI chat bubble for Website with OpenAI/Gemini backends and optional PDF grounding.",
    "version": "17.0.1.1.0",
    "category": "Website",
    "license": "LGPL-3",
    "author": "Your Company",
    "website": "https://example.com",
    "depends": ["base", "web", "website"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml"
    ],
    "assets": {
        "web.assets_frontend": [
            "website_ai_chat_min/static/src/css/ai_chat.css",
            "website_ai_chat_min/static/src/js/ai_chat.js"
        ]
    },
    "external_dependencies": {
        "python": ["pypdf", "openai", "google-generativeai"]
    },
    "installable": True,
    "application": False
}
