/* Public chat widget for Odoo Website
 * - Works without login (auth='public', csrf=False recommended)
 * - Gracefully handles CSRF if present
 * - Safe DOM (uses textContent)
 * - Typing indicator, disable/enable send, Enter to send, Esc to close
 */
(() => {
  "use strict";

  // --- I18N from optional helper node ---------------------------------------
  const i18nNode = document.getElementById("ai-chat-i18n");
  const I18N = i18nNode ? {
    title: i18nNode.dataset.title || "AI Chat",
    send: i18nNode.dataset.send || "Send",
    placeholder: i18nNode.dataset.placeholder || "Type your question…",
    close: i18nNode.dataset.close || "Close",
    privacy: i18nNode.dataset.privacy || "Privacy policy",
  } : {
    title: "AI Chat",
    send: "Send",
    placeholder: "Type your question…",
    close: "Close",
    privacy: "Privacy policy",
  };

  // --- Utilities -------------------------------------------------------------
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return m ? decodeURIComponent(m[2]) : "";
  };

  const withTimeout = (promise, ms = 15000) =>
    new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("timeout")), ms);
      promise.then(
        (v) => { clearTimeout(t); resolve(v); },
        (e) => { clearTimeout(t); reject(e); }
      );
    });

  // --- UI Factory ------------------------------------------------------------
  function buildUI(mount) {
    const wrap = document.createElement("div");
    wrap.className = "ai-chat-min__wrap";

    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.type = "button";
    bubble.setAttribute("aria-label", I18N.title);
    bubble.title = I18N.title;
    // Make the bubble visibly a chat button (older version had empty text)
    bubble.textContent = "💬";

    const panel = document.createElement("div");
    panel.className = "ai-chat-min__panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
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
    input.autocomplete = "off";
    input.spellcheck = true;

    const send = document.createElement("button");
    send.className = "ai-chat-min__send";
    send.type = "button";
    send.textContent = I18N.send;

    footer.appendChild(label);
    footer.appendChild(input);
    footer.appendChild(send);

    // Optional privacy link (rendered if present in page/template)
    const privacyUrlNode = document.querySelector("[data-ai-privacy-url]");
    if (privacyUrlNode && privacyUrlNode.dataset.aiPrivacyUrl) {
      const privacy = document.createElement("div");
      privacy.style.fontSize = "12px";
      privacy.style.opacity = "0.8";
      privacy.style.padding = "6px 10px";
      privacy.style.borderTop = "1px solid #eee";
      const a = document.createElement("a");
      a.target = "_blank";
      a.rel = "noopener";
      a.href = privacyUrlNode.dataset.aiPrivacyUrl;
      a.textContent = I18N.privacy;
      privacy.appendChild(a);
      panel.appendChild(privacy);
    }

    panel.appendChild(header);
    panel.appendChild(body);
    panel.appendChild(footer);
    wrap.appendChild(bubble);
    wrap.appendChild(panel);

    // --- Helpers -------------------------------------------------------------
    const scrollBottom = () => { body.scrollTop = body.scrollHeight; };

    function appendMessage(cls, text) {
      const row = document.createElement("div");
      row.className = `ai-chat-min__msg ${cls}`;
      row.textContent = String(text || "");
      body.appendChild(row);
      scrollBottom();
      return row;
    }

    let typingRow = null;
    function showTyping() {
      typingRow = document.createElement("div");
      typingRow.className = "ai-chat-min__msg bot";
      typingRow.textContent = "…";
      body.appendChild(typingRow);
      scrollBottom();
    }
    function hideTyping() {
      if (typingRow) {
        typingRow.remove();
        typingRow = null;
      }
    }

    function setSendingState(sending) {
      send.disabled = sending;
      input.disabled = sending;
      send.textContent = sending ? "…" : I18N.send;
    }

    function toggle(open) {
      panel.hidden = (open === undefined) ? !panel.hidden : !open;
      if (!panel.hidden) {
        input.focus();
      } else {
        bubble.focus();
      }
    }

    // --- Events --------------------------------------------------------------
    bubble.addEventListener("click", () => toggle(true));
    closeBtn.addEventListener("click", () => toggle(false));

    // Close on Esc
    window.addEventListener("keydown", (ev) => {
      if (!panel.hidden && ev.key === "Escape") {
        ev.preventDefault();
        toggle(false);
      }
    });

    async function sendMsg() {
      const q = input.value.trim();
      if (!q) return;

      appendMessage("user", q);
      input.value = "";
      setSendingState(true);
      showTyping();

      const headers = { "Content-Type": "application/json" };
      // If backend still enforces CSRF, attach token if available
      const token = getCookie("csrf_token") || getCookie("frontend_csrf_token") || "";
      if (token) {
        headers["X-Openerp-CSRF-Token"] = token;
        headers["X-CSRFToken"] = token;
      }

      try {
        const res = await withTimeout(fetch("/ai_chat/send", {
          method: "POST",
          headers,
          body: JSON.stringify({ question: q }),
          credentials: "same-origin",
        }), 15000);

        let data = null;
        try {
          data = await res.json();
        } catch (e) {
          console.error("AI Chat: JSON parse failed", e);
        }

        hideTyping();
        setSendingState(false);

        if (!res.ok || !data) {
          appendMessage("bot", "Network error.");
          return;
        }
        if (data.ok) {
          appendMessage("bot", data.reply || "");
        } else {
          appendMessage("bot", data.reply || "Error");
        }
      } catch (e) {
        console.error("AI Chat: send failed", e);
        hideTyping();
        setSendingState(false);
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

    // --- Mount & readiness check --------------------------------------------
    (mount || document.body).appendChild(wrap);

    // Public mode: we keep the bubble visible even if the readiness check fails.
    // Still, we probe the server for can_load to show console hints.
    (async () => {
      try {
        const res = await withTimeout(fetch("/ai_chat/can_load", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
          credentials: "same-origin",
        }), 8000);
        const data = await res.json();
        if (data && data.show === false) {
          // In public mode this should be true; if false, we log a hint instead of removing the widget.
          console.info("AI Chat: server returned show=false — check server toggle/public mode.");
        } else {
          console.info("AI Chat: widget ready.");
        }
      } catch (error) {
        console.warn("AI Chat: readiness check failed (network/CORS/CSP?) — widget left visible.", error);
      }
    })();
  }

  // Auto-mount
  document.addEventListener("DOMContentLoaded", () => {
    const standalone = document.querySelector("#ai-chat-standalone");
    if (standalone) {
      buildUI(standalone);
    } else {
      buildUI();
    }
  });
})();
console.log("AI Chat JS (public) loaded");
