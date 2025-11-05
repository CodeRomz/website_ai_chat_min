# -*- coding: utf-8 -*-
{
    "name": "Website AI Chat (Minimal)",
    "summary": "Minimal AI-powered website chat with PDF context (Gemini/OpenAI)",
    "description": "Adds an authenticated website chat widget that answers user questions using OpenAI or Google Gemini, grounded on PDFs from a server folder.",
    "version": "17.0.5.0.1",
    "category": "Website",
    "author": "CodeRomz / Your Company",
    "website": "https://example.com",
    "license": "LGPL-3",
    "depends": ["base", "website", "base_setup"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "website_ai_chat_min/static/src/css/ai_chat.css",
            "website_ai_chat_min/static/src/js/ai_chat.js",
        ],
    },
    "external_dependencies": {
        "python": ["pypdf", "openai", "google-generativeai"]
    },
    "installable": True,
    "application": False,
}
