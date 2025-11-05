# -*- coding: utf-8 -*-
{
    "name": "Website AI Chat (Minimal)",
    "summary": "Authenticated website AI chat with admin-configured provider, PDFs folder & guardrails",
    "version": "17.0.4.0.0",
    "license": "LGPL-3",
    "category": "Website",
    "author": "Your Company",
    "depends": ["website"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/settings.xml",
    ],
    "assets": {
        "website.assets_frontend": [
            "website_ai_chat_min/static/src/css/ai_chat.css",
            "website_ai_chat_min/static/src/js/ai_chat.js",
        ],
    },
    "installable": True,
    "application": False,
}
