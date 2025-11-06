/**
 * website_ai_chat_min - Frontend Chat Bubble (Odoo 17)
 * Vanilla JS: renders a chat bubble/panel, posts to /ai_chat/send, appends safe messages.
 */
(function () {
  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
    return null;
  }

  async function canLoad() {
    try {
      const r = await fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const j = await r.json();
      return !!(j && j.show);
    } catch (e) {
      console.warn("AI chat can_load failed:", e);
      return false;
    }
  }

  function mountUI() {
    if (document.getElementById("ai-chat-min-root")) return;
    const root = document.createElement("div");
    root.id = "ai-chat-min-root";
    root.className = "ai-chat-min__root";
    root.innerHTML = [
      '<div class="ai-chat-min__bubble" id="ai-chat-min-bubble" title="Chat">💬</div>',
      '<div class="ai-chat-min__panel" id="ai-chat-min-panel" style="display:none;">',
      '  <div class="ai-chat-min__header">AI Chat</div>',
      '  <div class="ai-chat-min__body" id="ai-chat-min-body"></div>',
      '  <div class="ai-chat-min__footer">',
      '    <input type="text" class="ai-chat-min__input" id="ai-chat-min-input" placeholder="Type your question..." />',
      '    <button class="ai-chat-min__send" id="ai-chat-min-send">Send</button>',
      "  </div>",
      "</div>",
    ].join("");
    document.body.appendChild(root);

    const bubble = document.getElementById("ai-chat-min-bubble");
    const panel = document.getElementById("ai-chat-min-panel");
    const input = document.getElementById("ai-chat-min-input");
    const sendBtn = document.getElementById("ai-chat-min-send");
    const body = document.getElementById("ai-chat-min-body");

    bubble.addEventListener("click", () => {
      const visible = panel.style.display !== "none";
      panel.style.display = visible ? "none" : "block";
      if (!visible) {
        setTimeout(() => input && input.focus(), 50);
      }
    });

    async function sendMessage() {
      const q = (input.value || "").trim();
      if (!q) return;
      appendMessage("user", q);
      input.value = "";
      const loadingId = appendMessage("bot", "…");

      try {
        const csrf =
          getCookie("csrf_token") ||
          getCookie("csrftoken") ||
          getCookie("CSRF-TOKEN") ||
          "";
        const r = await fetch("/ai_chat/send", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Openerp-CSRF-Token": csrf,
            "X-CSRFToken": csrf,
          },
          body: JSON.stringify({ question: q }),
        });
        const j = await r.json();
        removeMessage(loadingId);
        if (j && j.ok) {
          appendMessage("bot", j.reply || "");
        } else {
          appendMessage("bot", "Error: " + ((j && j.reply) || "Unknown error"));
        }
      } catch (e) {
        removeMessage(loadingId);
        appendMessage("bot", "Error: " + (e && e.message ? e.message : String(e)));
      }
    }

    sendBtn.addEventListener("click", sendMessage);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") sendMessage();
    });

    function appendMessage(role, text) {
      const id = "msg-" + Math.random().toString(36).slice(2);
      const el = document.createElement("div");
      el.className = "ai-chat-min__msg ai-chat-min__msg--" + role;
      el.id = id;
      el.textContent = String(text || ""); // safe against XSS
      body.appendChild(el);
      body.scrollTop = body.scrollHeight;
      return id;
    }

    function removeMessage(id) {
      const el = document.getElementById(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    }
  }

  document.addEventListener("DOMContentLoaded", async function () {
    if (await canLoad()) {
      mountUI();
    }
  });
})();
