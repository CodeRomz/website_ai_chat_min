# Website AI Chat (Minimal) — Odoo 17.0 (Full Fix)

## What’s included
- **Chat bubble** visible to Any logged-in user (not public).
- **Settings block** placed **after SEO** in *Settings → Website* as its **own group** (“AI Settings”). 
- Robust **CSRF**, **error handling**, and safe DOM updates (no XSS).
- Configurable **OpenAI / Gemini**, model, API key, documents folder, system instruction, allowed regex, context-only.
- External deps declared: `pypdf`, `openai`, `google-generativeai`.

## Install
1. Copy this folder to your addons path.
2. `pip install pypdf openai google-generativeai` in your Odoo env.
3. Update Apps and install/upgrade **Website AI Chat (Minimal)**.
4. Go to **Settings → Website → (below SEO) AI Settings** and configure.
5. Add yourself to **Website AI Chat / User** (or be Admin) to see the 💬 bubble.

## Notes
- Recommended OpenAI models: `gpt-3.5-turbo`, `gpt-4`.
- If *Answer Only From Documents* is enabled and no PDF context is found, the assistant replies: “I don’t know based on the current documents.”
