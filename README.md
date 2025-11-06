# Website AI Chat (Minimal) — Fixed for Odoo 17 CE

This build includes:
- Fixed group logic: admins (base.group_system) or users in `Website AI Chat / User` can see the chat bubble.
- Odoo 17 Settings UI block under **Website** (AI Chat section).
- Hardened controllers with robust error handling and CSRF headers.
- Safe frontend that avoids XSS by using textContent when injecting replies.
- External deps declared: pypdf, openai, google-generativeai.

## Install
1. Place the module folder in your addons path.
2. `pip install pypdf openai google-generativeai` in your Odoo environment.
3. Update Apps list, install **Website AI Chat (Minimal) - Fixed for Odoo 17 CE**.
4. Settings → Website → AI Chat (Minimal): set Provider, API Key, Model, Docs Folder.
5. Add yourself to **Website AI Chat / User** or be an Administrator to see the chat bubble.
