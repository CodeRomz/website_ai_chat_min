(() => {
  "use strict";

  // Currently selected Gemini model (from selector)
  let selectedModelName = null;

  // Front-end prompt usage tracker (per model, per browser session)
  const modelUsage = {};
  let modelSelectEl = null;
  let modelLimitLabelEl = null;

  // Per-browser, per-user GLOBAL chat memory (shared across models)
  const WAICM_STORAGE_PREFIX = "waicm_chat_v1_";
  const WAICM_MAX_HISTORY_MESSAGES = 6; // ~3 user/assistant exchanges
  let chatState = null;
  let storageKey = null;

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

  // Try to scope storage by Odoo user when available, fall back to session/anon
  function getUserScope() {
    try {
      if (window.odoo && odoo.session_info && typeof odoo.session_info.uid === "number") {
        return String(odoo.session_info.uid);
      }
    } catch (_) {
      // ignore
    }
    const sid = getCookie("session_id") || "";
    return sid ? `sess_${sid}` : "anon";
  }

  // GLOBAL chat storage key – independent of model
  function computeStorageKey() {
    const userScope = getUserScope();
    return `${WAICM_STORAGE_PREFIX}${userScope}_global`;
  }

  function saveChatState() {
    if (!window.localStorage || !storageKey || !chatState) {
      return;
    }
    try {
      chatState.updated = Date.now();
      window.localStorage.setItem(storageKey, JSON.stringify(chatState));
    } catch (_) {
      // ignore quota / private mode errors
    }
  }

  // Reset the SINGLE shared chat (all models share same history)
  function resetChat() {
    chatState = {
      version: 1,
      model: selectedModelName || null,
      messages: [],
      created: Date.now(),
      updated: Date.now(),
    };
    storageKey = computeStorageKey();
    saveChatState();
    body.innerHTML = "";
  }

  // Load the SINGLE shared chat from localStorage
  function loadChatState() {
    if (!window.localStorage) {
      chatState = null;
      storageKey = null;
      body.innerHTML = "";
      return;
    }
    storageKey = computeStorageKey();
    let parsed = null;
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (raw) {
        parsed = JSON.parse(raw);
      }
    } catch (_) {
      parsed = null;
    }
    if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.messages)) {
      chatState = {
        version: 1,
        model: selectedModelName || null,
        messages: [],
        created: Date.now(),
        updated: Date.now(),
      };
      saveChatState();
    } else {
      chatState = parsed;
    }
    rehydrateBodyFromState();
  }

  function pushUserMessageToState(text) {
    if (!chatState) {
      // initialise lazily if for some reason loadChatState was not called
      chatState = {
        version: 1,
        model: selectedModelName || null,
        messages: [],
        created: Date.now(),
        updated: Date.now(),
      };
      storageKey = computeStorageKey();
    }
    if (!Array.isArray(chatState.messages)) {
      chatState.messages = [];
    }
    chatState.messages.push({
      role: "user",
      content: String(text || ""),
      ts: Date.now(),
    });
    // Keep a bounded history for storage
    if (chatState.messages.length > 100) {
      chatState.messages = chatState.messages.slice(-100);
    }
    saveChatState();
  }

  function pushModelMessageToState(text) {
    if (!chatState) {
      chatState = {
        version: 1,
        model: selectedModelName || null,
        messages: [],
        created: Date.now(),
        updated: Date.now(),
      };
      storageKey = computeStorageKey();
    }
    if (!Array.isArray(chatState.messages)) {
      chatState.messages = [];
    }
    chatState.messages.push({
      role: "model",
      content: String(text || ""),
      ts: Date.now(),
    });
    if (chatState.messages.length > 100) {
      chatState.messages = chatState.messages.slice(-100);
    }
    saveChatState();
  }

  function buildPromptWithHistory(question) {
    const q = String(question || "").trim();
    if (!q) return "";
    if (!chatState || !Array.isArray(chatState.messages) || !chatState.messages.length) {
      return q;
    }

    const recent = chatState.messages.slice(-WAICM_MAX_HISTORY_MESSAGES);
    const lines = [];
    for (const msg of recent) {
      if (!msg || typeof msg.content !== "string") continue;
      const prefix = msg.role === "user" ? "User: " : "Assistant: ";
      lines.push(prefix + msg.content);
    }
    lines.push("");
    lines.push("User: " + q);
    lines.push("Assistant:");
    return lines.join("\n");
  }

  async function fetchJSON(
    url,
    { method = "GET", body = undefined, headers = {}, timeoutMs = 20000 } = {}
  ) {
    const ctrl = new AbortController();
    theTimeout: {
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

  // Logo in the bubble (keep your original logo)
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

  // New chat / reset button
  const resetBtn = document.createElement("button");
  resetBtn.className = "ai-chat-min__reset";
  resetBtn.setAttribute("type", "button");
  resetBtn.setAttribute("aria-label", "New chat");
  resetBtn.title = "New chat";
  resetBtn.textContent = "⟲";

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
  header.appendChild(resetBtn);
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

    // citations (optional)
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

    // suggestions (optional)
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

  function rehydrateBodyFromState() {
    body.innerHTML = "";
    if (!chatState || !Array.isArray(chatState.messages)) {
      return;
    }
    for (const msg of chatState.messages) {
      if (!msg || typeof msg.content !== "string") continue;
      if (msg.role === "user") {
        appendMessage("user", msg.content);
      } else if (msg.role === "model") {
        appendBotUI({
          answer_md: msg.content,
          citations: [],
          suggestions: [],
        });
      }
    }
  }

  // ---- MODEL USAGE HELPERS (dropdown + dynamic prompt count) ----

    function initModelUsage(models) {
    // Clear previous cache
    for (const key in modelUsage) {
      if (Object.prototype.hasOwnProperty.call(modelUsage, key)) {
        delete modelUsage[key];
      }
    }

    if (!Array.isArray(models)) {
      return;
    }

    for (const m of models) {
      if (!m || !m.model_name) {
        continue;
      }

      const code = m.model_name;

      // total limit from aic.user_quota_line
      const limit =
        typeof m.prompt_limit === "number" ? m.prompt_limit : null;

      // today's used prompts from aic.user_daily_usage (backend)
      let used = 0;
      const rawUsed = m.prompts_used;

      if (typeof rawUsed === "number" && !Number.isNaN(rawUsed)) {
        used = rawUsed;
      } else if (typeof rawUsed === "string") {
        const parsed = Number(rawUsed);
        if (!Number.isNaN(parsed)) {
          used = parsed;
        }
      }

      modelUsage[code] = {
        prompt_limit: limit,
        prompts_used: used,
      };
    }
  }


  function updatePromptCounterUI() {
    if (!modelLimitLabelEl) {
      return;
    }
    if (!selectedModelName || !modelUsage[selectedModelName]) {
      modelLimitLabelEl.textContent = "";
      return;
    }
    const meta = modelUsage[selectedModelName];
    const limit = meta.prompt_limit;
    const used = meta.prompts_used || 0;

    if (!limit || limit <= 0) {
      modelLimitLabelEl.textContent = "Prompts: unlimited";
    } else {
      modelLimitLabelEl.textContent = `Prompts: ${used}/${limit} used`;
    }
  }

  function incrementModelUsageForCurrentModel() {
    if (!selectedModelName || !modelUsage[selectedModelName]) {
      return;
    }
    const meta = modelUsage[selectedModelName];
    if (!meta.prompt_limit || meta.prompt_limit <= 0) {
      // unlimited – no need to track visually
      return;
    }
    meta.prompts_used = (meta.prompts_used || 0) + 1;
    updatePromptCounterUI();
  }

  // Render model selector + limits from /ai_chat/models
  function renderModels(models, defaultModel) {
    modelsBar.innerHTML = "";

    if (!Array.isArray(models) || !models.length) {
      modelsBar.style.display = "none";
      selectedModelName = null;
      // Still keep a shared chat history even if models can't be listed
      loadChatState();
      return;
    }

    modelsBar.style.display = "flex";

    initModelUsage(models);

    // Choose default: backend default or first model
    selectedModelName =
      defaultModel || (models[0] && models[0].model_name) || null;

    // Label on the left
    const labelEl = document.createElement("span");
    labelEl.className = "ai-chat-min__model-label";
    labelEl.textContent = "Model";

    // Dropdown with all models
    const selectEl = document.createElement("select");
    selectEl.className = "ai-chat-min__model-select";

    for (const m of models) {
      if (!m || !m.model_name) continue;
      const code = m.model_name;
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = code;
      selectEl.appendChild(opt);
    }

    // Align selectedModelName with an actual option
    if (selectedModelName && Array.from(selectEl.options).some(o => o.value === selectedModelName)) {
      selectEl.value = selectedModelName;
    } else if (selectEl.options.length) {
      selectEl.selectedIndex = 0;
      selectedModelName = selectEl.value;
    } else {
      selectedModelName = null;
    }

    // Dynamic prompt counter on the right
    const limitEl = document.createElement("span");
    limitEl.className = "ai-chat-min__model-limit";

    modelSelectEl = selectEl;
    modelLimitLabelEl = limitEl;
    updatePromptCounterUI();

    selectEl.addEventListener("change", () => {
      selectedModelName = selectEl.value || null;
      updatePromptCounterUI();
    });

    modelsBar.appendChild(labelEl);
    modelsBar.appendChild(selectEl);
    modelsBar.appendChild(limitEl);

    // Load shared chat history (same conversation regardless of model)
    loadChatState();
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

  // ---- New chat / Reset (GLOBAL, not per model) ----
  resetBtn.addEventListener("click", () => {
    const confirmed = window.confirm(
      "Start a new chat? This will clear the conversation in this browser."
    );
    if (!confirmed) return;
    resetChat();
  });

  // ---- SEND FLOW ----
  async function sendMsg() {
    const q = (input.value || "").trim();
    if (!q) return;

    // Append user message to UI + state
    appendMessage("user", q);
    pushUserMessageToState(q);
    input.value = "";
    send.disabled = true;

    // Build limited-context prompt for Gemini
    const fullPrompt = buildPromptWithHistory(q);
    const payloadQuestion = fullPrompt || q;

    try {
      const { ok, status, data } = await fetchJSON("/ai_chat/send", {
        method: "POST",
        body: {
          jsonrpc: "2.0",
          method: "call",
          params: {
            question: payloadQuestion,
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
        // Backend currently returns {ok, reply}; treat as markdown content.
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
        pushModelMessageToState(ui.answer_md);
        // Front-end dynamic counter (backend remains authoritative for real quota)
        incrementModelUsageForCurrentModel();
      } else {
        const fallback = (raw && raw.reply) || "Network error.";
        appendMessage("bot", fallback);
        // Do not push error messages into state to keep context clean
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
          // still allow global chat history, even if models are hidden
          loadChatState();
          return;
        }

        const raw = unwrap(data || {});
        if (ok && raw && raw.ok && Array.isArray(raw.models)) {
          renderModels(raw.models, raw.default_model || null);
        } else {
          modelsBar.style.display = "none";
          loadChatState();
        }
      } catch (e) {
        console.error("AI Chat: unable to load model limits", e);
        modelsBar.style.display = "none";
        loadChatState();
      }
    }
  })();
})();
