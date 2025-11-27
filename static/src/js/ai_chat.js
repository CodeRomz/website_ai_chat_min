(() => {
  "use strict";

  // Currently selected Gemini model (from chips)
  let selectedModelName = null;

  // Unwrap Odoo JSON-RPC envelopes
  const unwrap = (x) => (x && typeof x === "object" && "result" in x ? x.result : x);

  function getCookie(name) {
    const v = document.cookie.split("; ").find(r => r.startsWith(name + "="));
    return v ? decodeURIComponent(v.split("=")[1]) : "";
  }
  function getCsrf() {
    return getCookie("csrf_token") || "";
  }
  function getFrontendCsrf() {
    return getCookie("frontend_csrf_token") || "";
  }

  async function fetchJSON(
    url,
    { method = "GET", body = undefined, headers = {}, timeoutMs = 20000 } = {}
  ) {
    const ctrl = new AbortController();
    theTimeout:
    {
      var t = setTimeout(() => ctrl.abort(), timeoutMs);
    }
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
              ...(getFrontendCsrf() ? { "X-Frontend-CSRF-Token": getFrontendCsrf() } : {}),
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

  // ---- MOUNT CHECK ----
  async function canMount() {
    try {
      const { ok, status, data } = await fetchJSON("/ai_chat/can_load", {
        method: "POST",
        body: { jsonrpc: "2.0", method: "call", params: {} },
      });
      if (!ok && (status === 404 || status === 405)) return { mount: true, show: true };
      const raw = unwrap(data || {});
      return { mount: true, show: !!(raw && raw.show) };
    } catch {
      return { mount: true, show: true };
    }
  }

  // ---- UI ELEMENTS (scoped to .waicm) ----
  const wrap = document.createElement("div");
  wrap.className = "waicm"; // module root scope

  // Floating bubble
  const bubble = document.createElement("button");
  bubble.className = "ai-chat-min__bubble";
  bubble.setAttribute("type", "button");
  bubble.setAttribute("aria-label", "CodeRomz");

  // Logo in the bubble
  const icon = new Image();
  icon.src = "/website_ai_chat_min/static/src/img/chat_logo.png";
  icon.alt = "";
  icon.width = 45;
  icon.height = 45;
  icon.decoding = "async";
  icon.style.display = "block";
  icon.style.pointerEvents = "none";
  icon.addEventListener("error", () => { bubble.textContent = "💬"; });
  bubble.appendChild(icon);

  // Panel
  const panel = document.createElement("div");
  panel.className = "ai-chat-min__panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.hidden = true;

  // Header
  const header = document.createElement("div");
  header.className = "ai-chat-min__header";
  const title = document.createElement("div");
  title.textContent = "Academy Ai";

  // Maximize/Restore button
  const maxBtn = document.createElement("button");
  maxBtn.className = "ai-chat-min__max";
  maxBtn.setAttribute("type", "button");
  maxBtn.setAttribute("aria-label", "Maximize");
  maxBtn.title = "Maximize";
  maxBtn.textContent = "🗖";

  // Close button
  const closeBtn = document.createElement("button");
  closeBtn.className = "ai-chat-min__close";
  closeBtn.setAttribute("type", "button");
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "✕";

  header.appendChild(title);
  header.appendChild(maxBtn);
  header.appendChild(closeBtn);

  // Body (messages)
  const body = document.createElement("div");
  body.className = "ai-chat-min__body";

  // Models bar (per-user Gemini models)
  const modelsBar = document.createElement("div");
  modelsBar.className = "ai-chat-min__models";
  modelsBar.style.display = "none"; // hidden until data is loaded

  // Footer (input + send)
  const footer = document.createElement("div");
  footer.className = "ai-chat-min__footer";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Type your question...";
  const send = document.createElement("button");
  send.className = "ai-chat-min__send";
  send.setAttribute("type", "button");
  send.textContent = "Send";
  footer.appendChild(input);
  footer.appendChild(send);

  // Panel layout: header → models → body → footer
  panel.appendChild(header);
  panel.appendChild(modelsBar);
  panel.appendChild(body);
  panel.appendChild(footer);

  wrap.appendChild(bubble);
  wrap.appendChild(panel);
  document.body.appendChild(wrap);

  // ---- RENDER HELPERS ----
  function appendMessage(who, text) {
    const msg = document.createElement("div");
    msg.className = `ai-chat-min__msg ${who}`;
    msg.textContent = text;
    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }

  function appendBotUI(ui) {
    const msg = document.createElement("div");
    msg.className = "ai-chat-min__msg bot";

    // namespaced markdown container
    const md = document.createElement("div");
    md.className = "waicm-md waicm-box";
    md.innerHTML = ui.answer_md || "";
    msg.appendChild(md);

    // citations (optional) — namespaced classes
    if (Array.isArray(ui.citations) && ui.citations.length) {
      const c = document.createElement("div");
      c.className = "waicm-citations";
      for (const tag of ui.citations.slice(0, 6)) {
        const chip = document.createElement("span");
        chip.className = "waicm-chip";
        chip.textContent = String(tag || "");
        c.appendChild(chip);
      }
      msg.appendChild(c);
    }

    // suggestions (optional) — namespaced classes
    if (Array.isArray(ui.suggestions) && ui.suggestions.length) {
      const s = document.createElement("div");
      s.className = "waicm-suggestions";
      for (const sug of ui.suggestions.slice(0, 3)) {
        const pill = document.createElement("button");
        pill.className = "waicm-suggest";
        pill.type = "button";
        pill.textContent = String(sug || "");
        pill.addEventListener("click", () => {
          input.value = String(sug || "");
          input.focus();
        });
        s.appendChild(pill);
      }
      msg.appendChild(s);
    }

    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }

  // Render model chips + limits from /ai_chat/models
  function renderModels(models, defaultModel) {
    modelsBar.innerHTML = "";

    if (!Array.isArray(models) || !models.length) {
      modelsBar.style.display = "none";
      selectedModelName = null;
      return;
    }

    modelsBar.style.display = "flex";

    // Choose default: backend default or first model
    selectedModelName =
      defaultModel || (models[0] && models[0].model_name) || null;

    for (const m of models) {
      const code = m.model_name || "";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ai-chat-min__model";

      const label = document.createElement("div");
      label.textContent = code || "(no model)";
      btn.appendChild(label);

      const sub = document.createElement("span");
      const parts = [];
      if (typeof m.prompt_limit === "number") {
        parts.push(`Prompts: ${m.prompt_limit}`);
      }
      if (typeof m.tokens_per_prompt === "number") {
        parts.push(`Tokens: ${m.tokens_per_prompt}`);
      }
      sub.textContent = parts.join(" • ");
      if (sub.textContent) {
        btn.appendChild(sub);
      }

      if (selectedModelName && code === selectedModelName) {
        btn.classList.add("is-active");
      }

      btn.addEventListener("click", () => {
        selectedModelName = code || null;
        for (const child of modelsBar.querySelectorAll(".ai-chat-min__model")) {
          child.classList.toggle("is-active", child === btn);
        }
      });

      modelsBar.appendChild(btn);
    }
  }

  // ---- OPEN/CLOSE ----
  bubble.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) input.focus();
  });
  closeBtn.addEventListener("click", () => {
    panel.hidden = true;
  });

  // ---- Maximize / Restore ----
  maxBtn.addEventListener("click", () => {
    const isMax = panel.classList.toggle("is-max");
    if (isMax) {
      maxBtn.textContent = "🗗";
      maxBtn.title = "Restore";
      maxBtn.setAttribute("aria-label", "Restore");
    } else {
      maxBtn.textContent = "🗖";
      maxBtn.title = "Maximize";
      maxBtn.setAttribute("aria-label", "Maximize");
    }
  });

  // ---- SEND FLOW ----
  async function sendMsg() {
    const q = (input.value || "").trim();
    if (!q) return;
    appendMessage("user", q);
    input.value = "";
    send.disabled = true;

    try {
      const { ok, status, data } = await fetchJSON("/ai_chat/send", {
        method: "POST",
        body: {
          jsonrpc: "2.0",
          method: "call",
          params: {
            question: q,
            // pass selected Gemini model to backend
            model_name: selectedModelName,
          },
        },
        timeoutMs: 25000,
      });

      // If unauthorized (missing CSRF), hide UI gracefully
      if (!ok && (status === 401 || status === 403)) {
        panel.hidden = true;
        bubble.style.display = "none";
        return;
      }

      const raw = unwrap(data || {});
      if (ok && raw && raw.ok) {
        // Your current /ai_chat/send only returns {ok, reply},
        // so we treat reply as markdown content.
        const uiObj = (raw.ui && typeof raw.ui === "object") ? raw.ui : {};
        const answerText = uiObj.answer_md || raw.reply || "";
        const ui = {
          title: uiObj.title || "",
          summary: uiObj.summary || "",
          answer_md: String(answerText || ""),
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

  send.addEventListener("click", sendMsg);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMsg();
    }
  });

  // ---- Mount + Load models ----
  (async () => {
    const { mount, show } = await canMount();
    if (mount) {
      wrap.style.display = show ? "block" : "none";
      if (!show) {
        return;
      }

      // Load per-user Gemini models + limits from /ai_chat/models
      try {
        const { ok, status, data } = await fetchJSON("/ai_chat/models", {
          method: "POST",
          body: { jsonrpc: "2.0", method: "call", params: {} },
          timeoutMs: 15000,
        });

        if (!ok && (status === 401 || status === 403)) {
          modelsBar.style.display = "none";
          return;
        }

        const raw = unwrap(data || {});
        if (ok && raw && raw.ok && Array.isArray(raw.models)) {
          renderModels(raw.models, raw.default_model || null);
        } else {
          modelsBar.style.display = "none";
        }
      } catch (e) {
        console.error("AI Chat: unable to load model limits", e);
        modelsBar.style.display = "none";
      }
    }
  })();
})();
