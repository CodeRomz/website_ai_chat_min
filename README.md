# Website AI Chat (Minimal) – Odoo 17.0 CE

Minimal AI chat bubble for Odoo Website with OpenAI and Gemini backends, optional PDF grounding, GDPR-friendly defaults, and a standalone `/ai-chat` page for debugging.

## Features
- Standalone page: `/ai-chat` (auth='user')
- Bubble lazy-load on any website page (for authorized users only)
- Group-based access (`Website AI Chat / User`, `Website AI Chat / Admin`, admins also see it)
- Safe DOM rendering (`textContent`) to avoid XSS
- CSRF protection for JSON route, headers included in JS
- Optional PDF grounding from a server folder
- Ephemeral chat: **no server-side persistence**
- Configurable rate-limits (per session)

## Install
```
pip install openai google-generativeai pypdf
```

Copy `website_ai_chat_min` to your Odoo addons path, update apps, install.

## Configure
Website → Settings → **AI Settings** (below SEO). Set:
- **AI Provider**, **Model**, **API Key**
- Optional: **System Prompt**, **Allowed Questions (regex)**
- Optional: **PDF Folder** and **Answer Only From Documents**
- Optional: **Privacy Policy URL**
- Optional: **Rate Limiting**

## Logging (tail -f)
Server logs include structured lines with module prefix:
```
tail -f /var/log/odoo/odoo.log | grep website_ai_chat_min
```
- We never log raw questions (GDPR). We log caller uid/login, question length, provider, model, and exceptions with tracebacks.

## Security
- CSRF: enabled for `/ai_chat/send` and JS sends CSRF headers.
- Group checks on both `/ai_chat/can_load` and `/ai_chat/send`.
- No persistence and no raw question logging.

## Developer Reference
See **DEVELOPER_REFERENCE.md** for links and design notes.
