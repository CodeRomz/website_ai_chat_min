(() => {
  "use strict";

  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  function bodyReady(fn) {
    if (document.body) return fn();
    const obs = new MutationObserver(() => {
      if (document.body) { obs.disconnect(); fn(); }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }

  function buildUI(mount) {
    try {
      const wrap = document.createElement("div");
      wrap.className = "ai-chat-min__wrap";

      const bubble = document.createElement("button");
      bubble.className = "ai-chat-min__bubble";
      bubble.type = "button";
      bubble.setAttribute("aria-label", "AI Chat");
      bubble.textContent = "💬";

      const panel = document.createElement("div");
      panel.className = "ai-chat-min__panel";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.hidden = true;

      const header = document.createElement("div");
      header.className = "ai-chat-min__header";
      const title = document.createElement("span");
      title.textContent = "AI Chat";
      const closeBtn = document.createElement("button");
      closeBtn.className = "ai-chat-min__close";
      closeBtn.type = "button";
      closeBtn.textContent = "×";
      header.appendChild(title);
      header.appendChild(closeBtn);

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
      footer.appendChild(input);
      footer.appendChild(send);

      panel.appendChild(header);
      panel.appendChild(body);
      panel.appendChild(footer);

      wrap.appendChild(bubble);
      wrap.appendChild(panel);

      function toggle(open) {
        panel.hidden = (open === undefined) ? !panel.hidden : !open;
        (panel.hidden ? bubble : input).focus();
      }
      bubble.addEventListener("click", () => toggle(true));
      closeBtn.addEventListener("click", () => toggle(false));
      window.addEventListener("keydown", (ev) => { if (!panel.hidden && ev.key === "Escape") toggle(false); });

      function appendMessage(cls, text) {
        const row = document.createElement("div");
        row.className = `ai-chat-min__msg ${cls}`;
        row.textContent = String(text || "");
        body.appendChild(row);
        body.scrollTop = body.scrollHeight;
      }

      async function sendMsg() {
        const q = input.value.trim();
        if (!q) return;
        appendMessage("user", q);
        input.value = "";
        const headers = { "Content-Type": "application/json" };
        const token = (document.cookie.match(/(^| )csrf_token=([^;]+)/) || [])[2] ||
                      (document.cookie.match(/(^| )frontend_csrf_token=([^;]+)/) || [])[2];
        if (token) { headers["X-Openerp-CSRF-Token"] = token; headers["X-CSRFToken"] = token; }

        try {
          const res = await fetch("/ai_chat/send", {
            method: "POST",
            headers, body: JSON.stringify({ question: q }), credentials: "same-origin"
          });
          const raw = await res.json().catch(() => ({}));
          const data = unwrap(raw);
          if (res.ok && data && data.ok) appendMessage("bot", data.reply || "");
          else appendMessage("bot", (data && data.reply) || "Network error.");
        } catch (e) {
          console.error("AI Chat: send failed", e);
          appendMessage("bot", "Network error.");
        }
      }
      send.addEventListener("click", sendMsg);
      input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendMsg(); } });

      (mount || document.body).appendChild(wrap);

      // Readiness probe (keep bubble visible regardless)
      fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "same-origin",
      }).then(r => r.json())
        .then(raw => {
          const data = unwrap(raw);
          console.info("AI Chat: readiness:", data);
        })
        .catch(e => console.warn("AI Chat: readiness check failed", e));
    } catch (e) {
      console.error("AI Chat: buildUI failed", e);
    }
  }

  function init() {
    const standalone = document.querySelector("#ai-chat-standalone");
    buildUI(standalone || undefined);
  }

  // Ensure body exists even if assets load after DOMContentLoaded
  bodyReady(init);
})();