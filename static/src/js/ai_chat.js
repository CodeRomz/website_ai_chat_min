(() => {
  "use strict";

  // Unwrap Odoo JSON-RPC envelopes
  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  // Wait until <body> exists (assets can load after DOMContentLoaded on Website)
  function bodyReady(fn) {
    if (document.body) return fn();
    const mo = new MutationObserver(() => { if (document.body) { mo.disconnect(); fn(); } });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

  function getCookie(name) {
    const v = document.cookie.split("; ").find(r => r.startsWith(name + "="));
    return v ? decodeURIComponent(v.split("=")[1]) : "";
  }
  function getCsrf() {
    return getCookie("csrf_token") || getCookie("frontend_csrf_token") || "";
  }

  async function fetchJSON(url, { method = "GET", body = undefined, headers = {} } = {}) {
    const opts = {
      method,
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        ...(method !== "GET" ? { "Content-Type": "application/json", "X-CSRFToken": getCsrf(), "X-Openerp-CSRF-Token": getCsrf() } : {}),
        ...headers,
      },
    };
    if (body !== undefined) opts.body = typeof body === "string" ? body : JSON.stringify(body);
    const res = await fetch(url, opts);
    const isJSON = (res.headers.get("content-type") || "").includes("application/json");
    let data = null;
    try { data = isJSON ? await res.json() : null; } catch (_) {}
    return { ok: res.ok, status: res.status, data };
  }

  // ---- LOGIN CHECK (POST JSON-RPC) ----
  async function isUserLoggedIn() {
    const csrf = getCsrf();
    try {
      const res = await fetch("/web/session/get_session_info", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          ...(csrf ? { "X-CSRFToken": csrf, "X-Openerp-CSRF-Token": csrf } : {}),
        },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {} }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      const info = unwrap(data);
      return !!(info && Number.isInteger(info.uid) && info.uid > 0);
    } catch { return false; }
  }

  async function probeCanLoad() {
    try {
      const { ok, status, data } = await fetchJSON("/ai_chat/can_load", { method: "POST", body: {} });
      if (!ok && (status === 404 || status === 405)) return { mount: true };
      if (!ok && (status === 401 || status === 403)) return { mount: false };
      if (!ok) return { mount: true };
      const d = unwrap(data);
      if (d && typeof d === "object" && "show" in d) return { mount: !!d.show };
      return { mount: true };
    } catch { return { mount: true }; }
  }

  function buildUI(mount) {
    const wrap = document.createElement("div");
    wrap.className = "ai-chat-min__wrap";

    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.type = "button";
    bubble.setAttribute("aria-label", "Academy Ai");

    const icon = new Image();
    icon.src = "/website_ai_chat_min/static/src/img/chat_logo.png";
    icon.alt = "";
    icon.width = 45; icon.height = 45;
    icon.decoding = "async";
    icon.style.display = "block";
    icon.style.pointerEvents = "none";
    icon.addEventListener("error", () => { bubble.textContent = "💬"; });
    bubble.appendChild(icon);

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
    (mount || document.body).appendChild(wrap);

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

    function appendBox(el) {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";
      row.appendChild(el);
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    // ---- MINIMALIST ANSWER RENDERING ----
    function appendBotUI(ui) {
      const text = (ui && ui.answer_md ? String(ui.answer_md) : "").trim();

      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";
      row.textContent = text || "…";
      body.appendChild(row);

      // (Optional) tiny citations line; remove this block for zero extras
      if (ui && Array.isArray(ui.citations) && ui.citations.length) {
        const c = document.createElement("div");
        c.className = "ai-chat-min__msg bot";
        c.style.opacity = "0.8";
        c.style.fontSize = "12px";
        c.textContent = ui.citations.slice(0, 5).map(ci => `${ci.file} p.${ci.page}`).join(" • ");
        body.appendChild(c);
      }

      body.scrollTop = body.scrollHeight;
    }

    async function sendMsg() {
      const q = input.value.trim();
      if (!q) return;
      appendMessage("user", q);
      input.value = "";
      send.disabled = true;

      try {
        const { ok, status, data } = await fetchJSON("/ai_chat/send", {
          method: "POST",
          body: { question: q },
        });

        if (!ok && (status === 401 || status === 403)) {
          panel.hidden = true;
          bubble.style.display = "none";
          return;
        }

        const raw = unwrap(data || {});
        if (ok && raw && raw.ok) {
          if (raw.ui) appendBotUI(raw.ui);
          else appendMessage("bot", raw.reply || "");
        } else {
          appendMessage("bot", (raw && raw.reply) || "Network error.");
        }
      } catch (e) {
        console.error("AI Chat: send failed", e);
        appendMessage("bot", "Network error.");
      } finally {
        send.disabled = false;
      }
    }

    send.addEventListener("click", sendMsg);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
    });
  }

  async function init() {
    // Probe first (server decides visibility). If missing, rely on session check.
    const { mount } = await probeCanLoad();
    if (!mount) return;

    const logged = await isUserLoggedIn();
    if (!logged) return;

    const mountPoint = document.querySelector("#ai-chat-standalone");
    buildUI(mountPoint || undefined);
  }

  function boot() {
    try { init(); } catch (e) { console.warn("AI Chat init failed", e); }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
