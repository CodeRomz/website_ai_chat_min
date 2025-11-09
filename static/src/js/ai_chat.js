(() => {
  "use strict";
  // Unwrap Odoo JSON-RPC envelopes
  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

  function getCookie(name) {
    const v = document.cookie.split("; ").find(r => r.startsWith(name + "="));
    return v ? decodeURIComponent(v.split("=")[1]) : "";
  }
  function getCsrf() {
    return getCookie("csrf_token") || getCookie("frontend_csrf_token") || "";
  }

  async function fetchJSON(
  url,
  { method = "GET", body = undefined, headers = {}, timeoutMs = 12000 } = {}
) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  const opts = {
    method,
    credentials: "same-origin",
    signal: ctrl.signal,
    headers: {
      "Accept": "application/json",
      ...(method !== "GET"
        ? {
            "Content-Type": "application/json",
            "X-CSRFToken": getCsrf(),
            "X-Openerp-CSRF-Token": getCsrf(),
          }
        : {}),
      ...headers,
    },
  };
  if (body !== undefined) {
    opts.body = typeof body === "string" ? body : JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } finally {
    clearTimeout(t);
  }
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

    // Floating bubble that toggles the panel
    const bubble = document.createElement("button");
    bubble.className = "ai-chat-min__bubble";
    bubble.type = "button";
    bubble.setAttribute("aria-label", "Academy Ai");

    const icon = new Image();
    icon.src = "/website_ai_chat_min/static/src/img/chat_logo.png";
    icon.alt = "";
    icon.width = 45;
    icon.height = 45;
    icon.decoding = "async";
    icon.style.display = "block";
    icon.style.pointerEvents = "none";
    icon.addEventListener("error", () => {
      bubble.textContent = "💬";
    });
    bubble.appendChild(icon);

    // Main chat panel (dialog)
    const panel = document.createElement("div");
    panel.className = "ai-chat-min__panel minimal"; // minimal mode: hide titles/summaries by default
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.hidden = true;

    // Header with title and close button
    const header = document.createElement("div");
    header.className = "ai-chat-min__header";
    const title = document.createElement("span");
    title.textContent = "Academy Ai";
    const closeBtn = document.createElement("button");
    closeBtn.className = "ai-chat-min__close";
    closeBtn.type = "button";
    closeBtn.textContent = "×";
    header.appendChild(title);
    header.appendChild(closeBtn);


    // Body (message list)
    const body = document.createElement("div");
    body.className = "ai-chat-min__body";

    // Footer with input and send button
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

    // Assemble the panel without a docs-only toggle or banner
    panel.appendChild(header);
    panel.appendChild(body);
    panel.appendChild(footer);
    wrap.appendChild(bubble);
    wrap.appendChild(panel);
    (mount || document.body).appendChild(wrap);

    // No docs-only toggle; mode is determined by server configuration

    // Function to toggle open/close of panel
    function toggle(open) {
      panel.hidden = open === undefined ? !panel.hidden : !open;
      // Focus on bubble when closed; on input when open
      (panel.hidden ? bubble : input).focus();
    }
    bubble.addEventListener("click", () => toggle(true));
    closeBtn.addEventListener("click", () => toggle(false));
    window.addEventListener("keydown", (e) => {
      if (!panel.hidden && e.key === "Escape") toggle(false);
    });

    // Append a simple text message (user or fallback error)
    function appendMessage(cls, text) {
      const row = document.createElement("div");
      row.className = `ai-chat-min__msg ${cls}`;
      row.textContent = String(text || "");
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    // Clean trivial filler from model answers
    function cleanAnswerMd(s) {
      let t = String(s || "");
      // Remove some leading filler phrases
      t = t.replace(/^\s*acknowledged the greeting\.?\s*/i, "");
      t = t.replace(/^\s*acknowledged\.?\s*/i, "");
      t = t.replace(/^\s*(hi|hello|hey)[\s,!.-]*/i, "");
      return t.trim();
    }

    // Append a bot message with optional citations and suggestions
    function appendBotUI(ui) {
      const row = document.createElement("div");
      row.className = "ai-chat-min__msg bot";

      const box = document.createElement("div");
      box.className = "ai-box";

      if (ui && ui.title) {
        const t = document.createElement("div");
        t.className = "ai-chat-min__title";
        t.textContent = String(ui.title).trim().slice(0, 80);
        box.appendChild(t);
      }

      if (ui && ui.summary) {
        const s = document.createElement("div");
        s.className = "ai-chat-min__summary";
        s.textContent = String(ui.summary).trim();
        box.appendChild(s);
      }

      const a = document.createElement("div");
      a.className = "ai-md";
      a.innerHTML = mdLiteToHtml(cleanAnswerMd(ui?.answer_md || ""));
      box.appendChild(a);

      // Citations and suggestions are intentionally omitted for a cleaner UI

      row.appendChild(box);
      body.appendChild(row);
      body.scrollTop = body.scrollHeight;
    }

    // Send message to backend and handle response
    async function sendMsg() {
      const q = input.value.trim();
      if (!q) return;
      // Show the user's message immediately
      appendMessage("user", q);
      input.value = "";
      send.disabled = true;

      try {
        const { ok, status, data } = await fetchJSON("/ai_chat/send", {
          method: "POST",
          body: { question: q },
        });

        // If unauthorized, hide panel and bubble
        if (!ok && (status === 401 || status === 403)) {
          panel.hidden = true;
          bubble.style.display = "none";
          return;
        }

        const raw = unwrap(data || {});
        if (ok && raw && raw.ok) {
          // Build UI object from server response; prefer structured UI
          const uiObj = (raw.ui && typeof raw.ui === "object") ? raw.ui : {};
          const answerText = uiObj.answer_md || raw.reply || "";
          const ui = {
            title: uiObj.title || "",
            summary: uiObj.summary || "",
            answer_md: String(answerText).trim(),
            citations: Array.isArray(uiObj.citations) ? uiObj.citations : [],
            suggestions: Array.isArray(uiObj.suggestions) ? uiObj.suggestions.slice(0, 3) : [],
          };
          appendBotUI(ui);
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

    // Bind send on button click and Enter key
    send.addEventListener("click", sendMsg);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMsg();
      }
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


  // -- Tiny safe Markdown subset (bold **..**, italic *..*, `code`, -,*,1. lists) -> sanitized HTML
function mdLiteToHtml(md) {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const inline = t => (
    t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
     .replace(/`([^`]+)`/g, '<code>$1</code>')
     .replace(/(^|[^\\])\*([^*\n]+)\*/g, (m, p1, p2) => `${p1}<em>${p2}</em>`)
  );
  let s = esc(String(md || '')).replace(/\r\n?/g, '\n').trim();
  const lines = s.split('\n');
  const out = [];
  let inUl=false, inOl=false;
  const endLists=()=>{ if(inUl){out.push('</ul>'); inUl=false;} if(inOl){out.push('</ol>'); inOl=false;} };
  for (const raw of lines) {
    const l = raw.trim();
    const mUl = l.match(/^[*-]\s+(.*)$/);
    const mOl = l.match(/^\d+\.\s+(.*)$/);
    if (mUl) { if (inOl){out.push('</ol>'); inOl=false;} if(!inUl){out.push('<ul>'); inUl=true;} out.push('<li>'+inline(mUl[1])+'</li>'); continue; }
    if (mOl) { if (inUl){out.push('</ul>'); inUl=false;} if(!inOl){out.push('<ol>'); inOl=true;} out.push('<li>'+inline(mOl[1])+'</li>'); continue; }
    if (!l) { endLists(); continue; }
    endLists(); out.push('<p>'+inline(l)+'</p>');
  }
  endLists();
  return out.join('') || '<p>…</p>';
}

})();
