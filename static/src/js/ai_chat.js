(() => {
  "use strict";

  // Unwrap Odoo JSON-RPC envelopes
  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  // Wait until <body> exists (assets can load after DOMContentLoaded on Website)
  function bodyReady(fn) {
    if (document.body) return fn();
    const mo = new MutationObserver(() => {
      if (document.body) { mo.disconnect(); fn(); }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  const esc = (s) => String(s || "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

  function buildUI(mount) {
    const wrap = document.createElement("div");
    wrap.className = "ai-chat-min__wrap";

    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.type = "button";
    bubble.setAttribute("aria-label", "Academy Ai");
    bubble.textContent = "💬";

    const panel = document.createElement("div");
    panel.className = "ai-chat-min__panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.hidden = true;

    const header = document.createElement("div");
    header.className = "ai-chat-min__header";
    const title = document.createElement("span");
    title.textContent = "Academy Ai";
    const closeBtn = document.createElement("button");
    closeBtn.className = "ai-chat-min__close";
    closeBtn.type = "button";
    closeBtn.textContent = "×";
    header.appendChild(title); header.appendChild(closeBtn);

    const body = document.createElement("div");
    body.className = "ai-chat-min__body";

    const footer = document.createElement("div");
    footer.className = "ai-chat-min__footer";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Type your question…";
    const send = document.createElement("button");
    send.className = "ai-chat-min__send";
    send.type = "button";
    send.textContent = "Send";
    footer.appendChild(input); footer.appendChild(send);

    panel.appendChild(header); panel.appendChild(body); panel.appendChild(footer);
    wrap.appendChild(bubble); wrap.appendChild(panel);

    function toggle(open) {
      panel.hidden = (open === undefined) ? !panel.hidden : !open;
      (panel.hidden ? bubble : input).focus();
    }
    bubble.addEventListener("click", () => toggle(true));
    closeBtn.addEventListener("click", () => toggle(false));
    window.addEventListener("keydown", (e) => { if (!panel.hidden && e.key === "Escape") toggle(false); });

    function appendMessage(cls, text) {
      const row = document.createElement("div");
      row.className = `ai-chat-min__msg ${cls}`;
      row.textContent = String(text || "");
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    function appendBotUI(ui) {
      // Title
      if (ui.title) {
        const t = document.createElement("div");
        t.className = "ai-title";
        t.innerText = ui.title;
        appendBox(t);
      }
      // Summary
      if (ui.summary) {
        const s = document.createElement("div");
        s.className = "ai-summary";
        s.innerText = ui.summary;
        appendBox(s);
      }
      // Answer (clamped)
      if (ui.answer_md) {
        const md = document.createElement("div");
        md.className = "ai-md ai-md--clamp";
        md.innerText = ui.answer_md;  // keep plain for safety (no innerHTML)
        const more = document.createElement("div");
        more.className = "ai-more";
        more.innerText = "Show more";
        md.appendChild(more);
        more.addEventListener("click", () => {
          md.classList.toggle("ai-md--clamp");
          more.innerText = md.classList.contains("ai-md--clamp") ? "Show more" : "Show less";
        });
        appendBox(md);
      }
      // Citations
      if (Array.isArray(ui.citations) && ui.citations.length) {
        const c = document.createElement("div");
        c.className = "ai-citations";
        ui.citations.forEach(ci => {
          const chip = document.createElement("span");
          chip.className = "ai-chip";
          chip.innerText = `${ci.file} p.${ci.page}`;
          c.appendChild(chip);
        });
        appendBox(c);
      }
      // Suggestions
      if (Array.isArray(ui.suggestions) && ui.suggestions.length) {
        const s = document.createElement("div");
        s.className = "ai-suggestions";
        ui.suggestions.forEach(sug => {
          const b = document.createElement("button");
          b.className = "ai-suggest";
          b.type = "button";
          b.innerText = sug;
          b.addEventListener("click", () => { input.value = b.textContent || ""; input.focus(); });
          s.appendChild(b);
        });
        appendBox(s);
      }
    }

    function appendBox(el) {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";
      row.appendChild(el);
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    async function sendMsg() {
      const q = input.value.trim();
      if (!q) return;
      appendMessage("user", q);
      input.value = "";

      const headers = { "Content-Type": "application/json" };
      const t = (document.cookie.match(/(^| )csrf_token=([^;]+)/) || [])[2]
        || (document.cookie.match(/(^| )frontend_csrf_token=([^;]+)/) || [])[2];
      if (t) { headers["X-Openerp-CSRF-Token"] = t; headers["X-CSRFToken"] = t; }

      try {
        const res = await fetch("/ai_chat/send", {
          method: "POST",
          headers,
          body: JSON.stringify({ question: q }),
          credentials: "same-origin",
        });
        const raw = await res.json().catch(() => ({}));
        const data = unwrap(raw);
        if (res.ok && data && data.ok) {
          if (data.ui) appendBotUI(data.ui);
          else appendMessage("bot", data.reply || "");
        } else {
          appendMessage("bot", (data && data.reply) || "Network error.");
        }
      } catch (e) {
        console.error("AI Chat: send failed", e);
        appendMessage("bot", "Network error.");
      }
    }
    send.addEventListener("click", sendMsg);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
    });

    (mount || document.body).appendChild(wrap);
    console.info("AI Chat: mounted bubble");
  }

  async function init() {
    try {
      const res = await fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "same-origin",
      });
      const data = unwrap(await res.json());
      console.info("AI Chat: readiness:", data);
      if (data && data.show === true) {
        const standalone = document.querySelector("#ai-chat-standalone");
        buildUI(standalone || undefined);
      }
    } catch (e) {
      console.warn("AI Chat: can_load probe failed; keeping hidden", e);
    }
  }

  bodyReady(init);
})();
