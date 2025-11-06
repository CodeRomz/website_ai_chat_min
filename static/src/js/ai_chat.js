(() => {
  "use strict";

  const i18nNode = document.getElementById("ai-chat-i18n");
  const I18N = i18nNode ? {
    title: i18nNode.dataset.title || "AI Chat",
    send: i18nNode.dataset.send || "Send",
    placeholder: i18nNode.dataset.placeholder || "Type your question…",
    close: i18nNode.dataset.close || "Close",
  } : { title: "AI Chat", send: "Send", placeholder: "Type your question…", close: "Close" };

  const csrf = (name) => {
    const m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return m ? decodeURIComponent(m[2]) : "";
  };

  function buildUI(mount) {
    const wrap = document.createElement("div");
    wrap.className = "ai-chat-min__wrap";

    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.type = "button";
    bubble.setAttribute("aria-label", I18N.title);
    bubble.title = I18N.title;
    bubble.textContent = "💬";

    const panel = document.createElement("div");
    panel.className = "ai-chat-min__panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-live", "polite");
    panel.hidden = true;

    const header = document.createElement("div");
    header.className = "ai-chat-min__header";
    const h = document.createElement("span");
    h.textContent = I18N.title;
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "ai-chat-min__close";
    closeBtn.setAttribute("aria-label", I18N.close);
    closeBtn.textContent = "×";
    header.appendChild(h);
    header.appendChild(closeBtn);

    const body = document.createElement("div");
    body.className = "ai-chat-min__body";
    body.id = "ai-chat-min-body";
    body.setAttribute("aria-live", "polite");

    const footer = document.createElement("div");
    footer.className = "ai-chat-min__footer";

    const label = document.createElement("label");
    label.className = "sr-only";
    label.setAttribute("for", "ai-chat-min-input");
    label.textContent = I18N.placeholder;

    const input = document.createElement("input");
    input.id = "ai-chat-min-input";
    input.type = "text";
    input.placeholder = I18N.placeholder;
    input.autocapitalize = "sentences";

    const send = document.createElement("button");
    send.className = "ai-chat-min__send";
    send.type = "button";
    send.textContent = I18N.send;

    footer.appendChild(label);
    footer.appendChild(input);
    footer.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(body);
    panel.appendChild(footer);

    wrap.appendChild(bubble);
    wrap.appendChild(panel);

    function toggle(open) {
      panel.hidden = (open === undefined) ? !panel.hidden : !open;
      if (!panel.hidden) {
        input.focus();
      } else {
        bubble.focus();
      }
    }

    bubble.addEventListener("click", () => toggle(true));
    closeBtn.addEventListener("click", () => toggle(false));

    function appendMessage(cls, text) {
      const row = document.createElement("div");
      row.className = `ai-chat-min__msg ${cls}`;
      row.textContent = String(text || "");
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    async function canLoad() {
      await fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }).then(r => r.json()).then(d => {
        if (!d || d.show !== true) {
          wrap.remove();
        }
      }).catch(() => wrap.remove());
    }

    async function sendMsg() {
      const q = input.value.trim();
      if (!q) return;
      appendMessage("user", q);
      input.value = "";

      const token = csrf("csrf_token") || csrf("frontend_csrf_token") || "";
      try {
        const res = await fetch("/ai_chat/send", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Openerp-CSRF-Token": token,
            "X-CSRFToken": token
          },
          body: JSON.stringify({ question: q })
        });
        const data = await res.json();
        if (data && data.ok) {
          appendMessage("bot", data.reply || "");
        } else {
          appendMessage("bot", data && data.reply ? data.reply : "Error");
        }
      } catch (e) {
        appendMessage("bot", "Network error.");
      }
    }

    send.addEventListener("click", sendMsg);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        sendMsg();
      }
    });

    (mount || document.body).appendChild(wrap);
    canLoad();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const standalone = document.querySelector("#ai-chat-standalone");
    if (standalone) {
      buildUI(standalone);
    } else {
      buildUI();
    }
  });
})();
