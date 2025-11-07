/* eslint-disable no-console */
(() => {
  "use strict";

  // -----------------------------
  // Small utilities
  // -----------------------------

  // Unwrap JSON-RPC envelopes into {result: ...}
  function unwrap(x) {
    if (!x || typeof x !== "object") return x;
    if ("result" in x && x.result) return x.result;
    return x;
  }

  // Robust cookie getter (order/quoting safe enough for our use)
  function getCookie(name) {
    const parts = document.cookie.split(";").map((s) => s.trim());
    for (const p of parts) {
      if (p.startsWith(name + "=")) return decodeURIComponent(p.split("=", 2)[1] || "");
    }
    return "";
  }

  // Wait until <body> exists before mounting (Odoo Website loads can be late)
  function bodyReady(fn) {
    if (document.body) return void fn();
    const obs = new MutationObserver(() => {
      if (document.body) {
        obs.disconnect();
        fn();
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }

  // Escape helper (kept for future HTML renderers)
  function esc(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  // -----------------------------
  // UI construction
  // -----------------------------

  function buildUI(mount) {
    // Wrapper keeps bubble and panel positioned together
    const wrap = document.createElement("div");
    wrap.className = "ai-chat-min__wrap";
    wrap.setAttribute("aria-live", "polite");

    // Floating action button (chat bubble)
    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.setAttribute("type", "button");
    bubble.setAttribute("aria-label", "Open assistant");
    bubble.textContent = "💬";

    // Panel (dialog)
    const panel = document.createElement("div");
    panel.className = "ai-chat-min__panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.hidden = true; // toggled via toggle()

    // Header
    const header = document.createElement("div");
    header.className = "ai-chat-min__header";
    header.id = "ai-chat-min-header";

    const title = document.createElement("div");
    title.className = "ai-chat-min__title";
    title.textContent = "Assistant";

    const close = document.createElement("button");
    close.className = "ai-chat-min__close";
    close.setAttribute("type", "button");
    close.setAttribute("aria-label", "Close assistant");
    close.textContent = "✕";

    header.append(title, close);
    panel.setAttribute("aria-labelledby", "ai-chat-min-header");

    // Body (messages stream)
    const body = document.createElement("div");
    body.className = "ai-chat-min__body";

    // Footer (input + send)
    const footer = document.createElement("div");
    footer.className = "ai-chat-min__footer";

    const input = document.createElement("textarea");
    input.className = "ai-chat-min__input";
    input.setAttribute("rows", "1");
    input.setAttribute("placeholder", "Type your question…");

    const send = document.createElement("button");
    send.className = "ai-chat-min__send";
    send.setAttribute("type", "button");
    send.textContent = "Send";

    footer.append(input, send);

    // Assemble
    panel.append(header, body, footer);
    wrap.append(bubble, panel);
    mount.append(wrap);

    // Focus management & accessibility
    let isOpen = false;
    let busy = false;

    function toggle(open) {
      isOpen = open;
      panel.hidden = !open;
      if (open) {
        panel.scrollTop = panel.scrollHeight;
        input.focus();
      } else {
        bubble.focus();
      }
    }

    // Focus trap inside the dialog when open
    function focusTrap(e) {
      if (!isOpen) return;
      if (e.key !== "Tab") return;
      const focusables = panel.querySelectorAll(
        'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'
      );
      if (!focusables.length) return;
      const list = Array.from(focusables).filter((el) => !el.hasAttribute("disabled"));
      const first = list[0];
      const last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        last.focus();
        e.preventDefault();
      } else if (!e.shiftKey && document.activeElement === last) {
        first.focus();
        e.preventDefault();
      }
    }

    // Messages helpers
    function appendMessage(kind, text) {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg " + (kind === "user" ? "user" : "bot");
      const bubble = document.createElement("div");
      bubble.className = "ai-chat-min__msgbubble";
      bubble.textContent = text || "";
      row.append(bubble);
      body.append(row);
      body.scrollTop = body.scrollHeight;
      return row; // return for further updates (e.g., typing)
    }

    function appendTyping() {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";
      const bubble = document.createElement("div");
      bubble.className = "ai-chat-min__msgbubble";
      bubble.textContent = "Assistant is typing…";
      bubble.setAttribute("data-typing", "1");
      row.append(bubble);
      body.append(row);
      body.scrollTop = body.scrollHeight;
      return bubble;
    }

    function replaceTyping(node, withText) {
      if (node && node.getAttribute("data-typing")) {
        node.removeAttribute("data-typing");
        node.textContent = withText || "";
      }
    }

    function chip(text) {
      const c = document.createElement("span");
      c.className = "ai-chat-min__chip";
      c.textContent = text;
      return c;
    }

    // Render a structured bot UI (safe: all textContent)
    function appendBotUI(ui) {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";
      const b = document.createElement("div");
      b.className = "ai-chat-min__msgbubble";

      if (ui.title) {
        const t = document.createElement("div");
        t.className = "ai-chat-min__ui-title";
        t.textContent = ui.title;
        b.append(t);
      }
      if (ui.summary) {
        const s = document.createElement("div");
        s.className = "ai-chat-min__ui-summary";
        s.textContent = ui.summary;
        b.append(s);
      }
      if (ui.answer_md || ui.answer_text) {
        const a = document.createElement("div");
        a.className = "ai-chat-min__ui-answer clamp";
        // We keep plain text (no HTML parsing) to avoid XSS risk.
        a.textContent = ui.answer_md || ui.answer_text;
        const more = document.createElement("button");
        more.type = "button";
        more.className = "ai-chat-min__more";
        more.textContent = "Show more";
        more.addEventListener("click", () => {
          const clamped = a.classList.toggle("clamp");
          more.textContent = clamped ? "Show more" : "Show less";
        });
        b.append(a, more);
      }
      if (Array.isArray(ui.citations) && ui.citations.length) {
        const ct = document.createElement("div");
        ct.className = "ai-chat-min__citations";
        const label = document.createElement("div");
        label.className = "ai-chat-min__citlabel";
        label.textContent = ui.citations_label || "Citations";
        ct.append(label);
        for (const c of ui.citations) {
          ct.append(chip(`${c.file || "doc"} p.${c.page || "?"}`));
        }
        b.append(ct);
      }
      if (Array.isArray(ui.suggestions) && ui.suggestions.length) {
        const sg = document.createElement("div");
        sg.className = "ai-chat-min__sugs";
        for (const s of ui.suggestions) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ai-chat-min__sug";
          btn.textContent = s;
          btn.addEventListener("click", () => {
            input.value = s;
            input.focus();
          });
          sg.append(btn);
        }
        b.append(sg);
      }

      row.append(b);
      body.append(row);
      body.scrollTop = body.scrollHeight;
    }

    // Send logic
    async function sendMsg() {
      const q = (input.value || "").trim();
      if (!q || busy) return;

      busy = true;
      input.disabled = true;
      send.disabled = true;

      appendMessage("user", q);
      input.value = "";

      // Visual typing indicator
      const typingNode = appendTyping();

      try {
        const headers = { "Content-Type": "application/json" };
        const t = getCookie("csrf_token") || getCookie("frontend_csrf_token");
        if (t) {
          headers["X-Openerp-CSRF-Token"] = t;
          headers["X-CSRFToken"] = t;
        }

        const res = await fetch("/ai_chat/send", {
          method: "POST",
          credentials: "same-origin",
          headers,
          body: JSON.stringify({ question: q }),
        });

        // Gracefully handle non-JSON bodies
        const raw = await res
          .json()
          .catch(() => ({}));

        const rpcError =
          raw && raw.error && (raw.error.message || (raw.error.data && raw.error.data.message));
        const data = unwrap(raw);

        if (res.status === 401 || res.status === 403 || rpcError) {
          replaceTyping(typingNode, rpcError || "You are not allowed to use this assistant.");
          return;
        }
        if (res.status === 429) {
          replaceTyping(typingNode, "You are sending messages too quickly. Please wait a moment.");
          return;
        }

        if (res.ok && data) {
          if (data.ok && data.ui) {
            // Remove typing row before rendering structured UI
            replaceTyping(typingNode, "");
            typingNode.parentElement?.parentElement?.remove?.();
            appendBotUI(data.ui);
          } else if (data.ok) {
            replaceTyping(typingNode, data.reply || "");
          } else {
            replaceTyping(typingNode, data.reply || "The service is temporarily unavailable.");
          }
        } else {
          replaceTyping(typingNode, "Network error. Please try again.");
        }
      } catch (e) {
        console.error(e);
        replaceTyping(typingNode, "Network error. Please try again.");
      } finally {
        busy = false;
        input.disabled = false;
        send.disabled = false;
        input.focus();
      }
    }

    // Events
    bubble.addEventListener("click", () => toggle(true));
    close.addEventListener("click", () => toggle(false));
    panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        toggle(false);
      } else {
        focusTrap(e);
      }
    });
    send.addEventListener("click", sendMsg);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMsg();
      }
    });

    // expose for debugging if needed
    return { toggle, input, send, body };
  }

  // -----------------------------
  // Initialization
  // -----------------------------
  async function init() {
    try {
      const res = await fetch("/ai_chat/can_load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: "{}",
      });
      const raw = await res.json().catch(() => ({}));
      const data = unwrap(raw) || {};
      if (data && data.show === true) {
        const mount = document.getElementById("ai-chat-standalone") || document.body;
        buildUI(mount);
        console.info("[ai-chat] widget mounted");
      } else {
        console.info("[ai-chat] widget hidden by policy");
      }
    } catch (e) {
      console.warn("[ai-chat] cannot probe can_load:", e);
    }
  }

  bodyReady(init);
})();
