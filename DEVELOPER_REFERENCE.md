# Developer Reference

Authoritative sources informing this module’s design and code:

1. **Odoo 17.0 `website_livechat`**  
   https://github.com/odoo/odoo/tree/17.0/addons/website_livechat

2. **Odoo 17.0 `website`**  
   https://github.com/odoo/odoo/tree/17.0/addons/website

3. **Google Gemini API – Text Generation Docs**  
   https://ai.google.dev/gemini-api/docs

4. **OpenAI – Text/Chat Completions Guide**  
   https://platform.openai.com/docs/guides/text

## Notes

- Settings view is inserted **after Website ▶ SEO** using robust XPath anchor by `@name='seo'` (fallback `@string='SEO'`).
- Controllers follow Odoo patterns for website JSON routes; `/ai_chat/send` uses `csrf=True`, JS sets `X-Openerp-CSRF-Token` and `X-CSRFToken` headers.
- **No raw questions** are logged (GDPR). Use `tail -f /var/log/odoo/odoo.log | grep website_ai_chat_min` to observe server behavior.
- Frontend is vanilla JS (no OWL), uses `textContent` for safe rendering, has basic a11y attributes.
