/*!
 * Website AI Chat (minimal widget)
 * - Preserves existing names used by the template
 * - Adds robust JSON salvage for Gemini/OpenAI odd responses
 * - Keeps "minimal" panel by default; expands when citations are present
 */

(function () {
  "use strict";

  // ---------- DOM refs created at boot ----------
  let panel, body, input, send;

  // ---------- Boot ----------
  function boot() {
    init();
  }

  function init() {
    probeCanLoad().then(ok => {
      if (!ok) return;
      buildUI();
    });
  }

  // ---------- Probes ----------
  async function isUserLoggedIn() {
    try {
      const r = await fetchJSON("/web/session/get_session_info", { method: "POST" });
      return !!(r && r.session_id && r.uid);
    } catch { return false; }
  }

  async function probeCanLoad() {
    const logged = await isUserLoggedIn();
    if (!logged) return false;
    try {
      const r = await fetchJSON("/ai_chat/can_load", { method: "POST" });
      return !!(r && r.show);
    } catch { return false; }
  }

  // ---------- UI ----------
  function buildUI() {
    // container
    panel = document.createElement("div");
    panel.className = "ai-chat-min__panel minimal";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");

    const header = document.createElement("div");
    header.className = "ai-chat-min__header";
    header.innerHTML = `<div class="ai-chat-min__titlebar">Academy Ai</div>
                        <button class="ai-chat-min__close" aria-label="Close">×</button>`;

    body = document.createElement("div");
    body.className = "ai-chat-min__body";

    const footer = document.createElement("div");
    footer.className = "ai-chat-min__footer";

    input = document.createElement("input");
    input.className = "ai-chat-min__input";
    input.setAttribute("placeholder", "Type your question...");
    input.setAttribute("aria-label", "Question");

    send = document.createElement("button");
    send.className = "ai-chat-min__send";
    send.textContent = "Send";

    footer.appendChild(input);
    footer.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(body);
    panel.appendChild(footer);
    document.body.appendChild(panel);

    // events
    header.querySelector(".ai-chat-min__close").addEventListener("click", () => {
      panel.remove();
    });
    send.addEventListener("click", () => sendMsg());
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        sendMsg();
      }
    });
  }

  function appendMessage(who, text) {
    const row = document.createElement("div");
    row.className = `ai-chat-min__msg ${who}`;

    const bubble = document.createElement("div");
    bubble.className = "ai-box";
    bubble.textContent = String(text || "");
    row.appendChild(bubble);
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  function cleanAnswerMd(s) {
    let t = String(s || "");
    t = t.replace(/^\s*acknowledged( the greeting)?\.?\s*/i, "");
    t = t.replace(/^\s*(hi|hello|hey)[\s,!.-]*/i, "");
    return t.trim();
  }

  function mdLiteToHtml(s) {
    // very small subset: bold, italic, inline code, lists, line breaks
    let h = String(s || "");
    h = h.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    // simple lists
    h = h.replace(/^\s*-\s+(.*)$/gim, "<li>$1</li>");
    h = h.replace(/(<li>.*<\/li>)/gims, "<ul>$1</ul>");
    // paragraph breaks
    h = h.replace(/\n{2,}/g, "<br/>");
    return h;
  }

  function extractJsonSafe(s) {
    if (!s) return null;
    try { return JSON.parse(s); } catch {}
    // fenced
    try {
      const stripped = stripFences(s);
      if (stripped) return JSON.parse(stripped);
    } catch {}
    // balanced braces
    try {
      const inner = findBalancedObject(s);
      if (inner) return JSON.parse(inner);
    } catch {}
    return null;
  }

  function stripFences(s) {
    s = String(s || "").trim();
    if (s.startsWith("```")) {
      s = s.split("\n", 1)[1] || "";
      if (s.includes("```")) s = s.split("```").slice(0, -1).join("```");
    }
    return s.trim();
  }

  function findBalancedObject(s) {
    s = String(s || "");
    const start = s.indexOf("{");
    if (start < 0) return null;
    let depth = 0;
    for (let i = start; i < s.length; i++) {
      const ch = s[i];
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) return s.slice(start, i + 1);
      }
    }
    return null;
  }

  // ---------- Rendering ----------
  function appendBotUI(ui) {
    // First pass: if answer_md actually contains the JSON object, salvage it
    try {
      const maybe = extractJsonSafe(ui?.answer_md || "");
      if (maybe && (maybe.answer_md || maybe.summary || maybe.title)) {
        ui = {
          title: String(maybe.title || ui.title || "").slice(0, 80),
          summary: String(maybe.summary || ui.summary || ""),
          answer_md: String(maybe.answer_md || maybe.text || ""),
          citations: Array.isArray(maybe.citations) ? maybe.citations : (ui.citations || []),
          suggestions: Array.isArray(maybe.suggestions) ? maybe.suggestions.slice(0, 3) : (ui.suggestions || []),
        };
      }
    } catch {}

    // Final safety: if answer_md still looks like an object, pluck the fields
    if (typeof ui?.answer_md === "string" && ui.answer_md.trim().startsWith("{")) {
      const looseGet = (src, key) => {
        const re = new RegExp(`"${key}"\\s*:\\s*"(.*?)"`, "is");
        const m = re.exec(String(src));
        return m ? m[1].replace(/\r/g, "") : null;
      };
      const am = looseGet(ui.answer_md, "answer_md");
      if (am) ui.answer_md = am;
      const tt = looseGet(ui.answer_md, "title");     if (tt && !ui.title)   ui.title = tt.slice(0, 80);
      const ss = looseGet(ui.answer_md, "summary");   if (ss && !ui.summary) ui.summary = ss;
    }

    // Minimal mode unless there are citations (doc answer)
    if (Array.isArray(ui?.citations) && ui.citations.length > 0) {
      panel.classList.remove("minimal");
    } else {
      panel.classList.add("minimal");
    }

    const row = document.createElement("div");
    row.className = "ai-chat-min__msg bot";

    const box = document.createElement("div");
    box.className = "ai-box";

    if (ui?.title) {
      const t = document.createElement("div");
      t.className = "ai-chat-min__title";
      t.textContent = String(ui.title).trim().slice(0, 80);
      box.appendChild(t);
    }

    if (ui?.summary) {
      const s = document.createElement("div");
      s.className = "ai-chat-min__summary";
      s.textContent = String(ui.summary).trim();
      box.appendChild(s);
    }

    const a = document.createElement("div");
    a.className = "ai-md";
    a.innerHTML = mdLiteToHtml(cleanAnswerMd(ui?.answer_md || ""));
    box.appendChild(a);

    if (Array.isArray(ui?.citations) && ui.citations.length) {
      const cwrap = document.createElement("div");
      cwrap.className = "ai-citations";
      ui.citations.slice(0, 5).forEach(ci => {
        const chip = document.createElement("span");
        chip.className = "ai-chip";
        chip.textContent = `${ci.file} p.${ci.page}`;
        cwrap.appendChild(chip);
      });
      box.appendChild(cwrap);
    }

    row.appendChild(box);
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  // ---------- Send ----------
  async function sendMsg() {
    const q = String(input.value || "").trim();
    if (!q) return;
    appendMessage("user", q);
    input.value = "";
    send.disabled = true;
    try {
      const data = await fetchJSON("/ai_chat/send", {
        method: "POST",
        body: { question: q }
      });
      const raw = data || {};
      if (raw && raw.ui) {
        appendBotUI(raw.ui);
      } else if (raw && raw.reply) {
        appendBotUI({ title: "", summary: "", answer_md: raw.reply, citations: [], suggestions: [] });
      } else {
        appendBotUI({ answer_md: "No answer returned.", citations: [], suggestions: [] });
      }
    } catch (e) {
      appendBotUI({ answer_md: "The AI service is temporarily unavailable. Please try again shortly.", citations: [], suggestions: [] });
    } finally {
      send.disabled = false;
      input.focus();
    }
  }

  // ---------- Fetch helper with CSRF ----------
  async function fetchJSON(url, opts) {
    opts = opts || {};
    const method = (opts.method || "GET").toUpperCase();
    const headers = { Accept: "application/json" };
    const csrf = getCookie("csrf_token") || getCookie("CSRF-TOKEN") || getCookie("X-Openerp-CSRF-Token");
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      if (csrf) {
        headers["X-CSRFToken"] = csrf;
        headers["X-Openerp-CSRF-Token"] = csrf;
      }
    }
    const body = opts.body ? JSON.stringify(opts.body) : undefined;
    const r = await fetch(url, { method, headers, body, credentials: "include" });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      return await r.json();
    }
    return null;
  }

  function getCookie(name) {
    const parts = (`; ${document.cookie}`).split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(";").shift();
    return null;
  }

  // Kick it off
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
