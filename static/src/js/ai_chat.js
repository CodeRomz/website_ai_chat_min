/** website_ai_chat_min — robust widget with privacy link */
(() => {
  "use strict";

  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  function bodyReady(fn) {
    if (document.body) return fn();
    const mo = new MutationObserver(() => {
      if (document.body) { mo.disconnect(); fn(); }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  const STATE = {
    userPublic: false,
    loginUrl: "/web/login",
    privacyUrl: "",
    open: false,
    sending: false,
  };

  function el(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    return tpl.content.firstElementChild;
  }

  function appendMessage(container, who, text) {
    const msg = el(`<div class="ai-chat-min__msg ${who}"></div>`);
    msg.textContent = String(text || "");
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function buildUI() {
    const wrap = el(`<div class="ai-chat-min__wrap" id="ai-chat-standalone"></div>`);
    const bubble = el(`<button class="ai-chat-min__bubble" aria-label="Open AI Chat" title="Chat">💬</button>`);
    wrap.appendChild(bubble);

    const panel = el(`
      <section class="ai-chat-min__panel" hidden>
        <header class="ai-chat-min__header">
          <div>AI Chat</div>
          <button class="ai-chat-min__close" aria-label="Close">×</button>
        </header>
        <div class="ai-chat-min__body"></div>
        <footer class="ai-chat-min__footer">
          <input type="text" placeholder="Type a message…" maxlength="1000"/>
          <button class="ai-chat-min__send">Send</button>
        </footer>
      </section>
    `);
    wrap.appendChild(panel);
    document.body.appendChild(wrap);

    const closeBtn = panel.querySelector(".ai-chat-min__close");
    const bodyEl   = panel.querySelector(".ai-chat-min__body");
    const inputEl  = panel.querySelector("input");
    const sendBtn  = panel.querySelector(".ai-chat-min__send");

    // Optional privacy link
    if (STATE.privacyUrl) {
      const link = el(`<div style="padding:8px 12px;font-size:12px;">
        <a href="${STATE.privacyUrl}" target="_blank" rel="noopener">Privacy Policy</a>
      </div>`);
      panel.appendChild(link);
    }

    if (STATE.userPublic) {
      inputEl.disabled = true;
      inputEl.placeholder = "Sign in to start chatting…";
      sendBtn.textContent = "Sign in";
      sendBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        window.location.href = STATE.loginUrl || "/web/login";
      });
      const header = panel.querySelector(".ai-chat-min__header");
      header.insertAdjacentHTML("beforeend", `<span style="margin-left:8px;opacity:.85">(guest)</span>`);
    } else {
      const doSend = async () => {
        if (STATE.sending) return;
        const val = (inputEl.value || "").trim();
        if (!val) return;
        STATE.sending = true;
        try {
          appendMessage(bodyEl, "user", val);
          inputEl.value = "";
          const resp = await fetch("/ai_chat/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ prompt: val }),
          });
          const data = unwrap(await resp.json()) || {};
          if (data.ok) appendMessage(bodyEl, "bot", data.reply || "");
          else appendMessage(bodyEl, "bot", data.error || "Error.");
        } catch (e) {
          appendMessage(bodyEl, "bot", "Network error. Please try again.");
        } finally {
          STATE.sending = false;
        }
      };
      sendBtn.addEventListener("click", (e) => { e.preventDefault(); doSend(); });
      inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
      });
    }

    const toggle = (open) => {
      STATE.open = (open == null ? !STATE.open : !!open);
      panel.toggleAttribute("hidden", !STATE.open);
    };
    bubble.addEventListener("click", () => toggle(true));
    closeBtn.addEventListener("click", () => toggle(false));
  }

  async function init() {
    try {
      const resp = await fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "same-origin",
      });
      const data = unwrap(await resp.json()) || {};
      STATE.userPublic = !!data.user_public;
      STATE.loginUrl = data.login_url || "/web/login";
      STATE.privacyUrl = data.privacy_url || "";

      if (data && data.show === true) buildUI();
    } catch (e) {
      // keep hidden
    }
  }

  bodyReady(init);
})();
