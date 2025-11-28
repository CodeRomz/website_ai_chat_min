(() => {
  "use strict";

  // Currently selected Gemini model (from dropdown)
  let selectedModelName = null;

  // Front-end model usage tracker (per model, per browser session)
  const modelUsage = {};
  let modelSelectEl = null;
  let modelLimitLabelEl = null;

  // Per-browser, per-user global chat memory (shared across models)
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

  function getUserScope() {
    // Try to scope LS key by logged-in user id; fallback to "anon"
    const uid = getCookie("frontend_lang_uid") || getCookie("session_id") || "";
    if (uid) {
      return uid;
    }
    const sid = getCookie("session_id") || "";
    return sid ? `sess_${sid}` : "anon";
  }

  function computeStorageKey() {
    const userScope = getUserScope();
    // Single shared history per user/browser, independent of model
    return `${WAICM_STORAGE_PREFIX}${userScope}_global`;
  }

  function saveChatState() {
    if (!window.localStorage || !storageKey || !chatState) {
      return;
    }
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(chatState));
    } catch (e) {
      // Best-effort only; ignore quota errors
      console.warn("AI Chat: unable to persist chat state", e);
    }
  }

  function rehydrateBodyFromState() {
    body.innerHTML = "";
    if (!chatState || !Array.isArray(chatState.messages)) return;
    for (const msg of chatState.messages) {
      if (!msg || typeof msg !== "object") continue;
      if (msg.role === "user") {
        appendMessage("user", msg.content || "");
      } else if (msg.role === "assistant") {
        appendMessage("bot", msg.content || "");
      }
    }
  }

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
    if (!chatState || !Array.isArray(chatState.messages)) return;
    chatState.messages.push({
      role: "user",
      content: String(text || ""),
      ts: Date.now(),
    });
    if (chatState.messages.length > 100) {
      chatState.messages = chatState.messages.slice(-100);
    }
    chatState.updated = Date.now();
    saveChatState();
  }

  function pushModelMessageToState(text) {
    if (!chatState || !Array.isArray(chatState.messages)) return;
    chatState.messages.push({
      role: "assistant",
      content: String(text || ""),
      ts: Date.now(),
    });
    if (chatState.messages.length > 100) {
      chatState.messages = chatState.messages.slice(-100);
    }
    chatState.updated = Date.now();
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
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const finalHeaders = Object.assign(
        {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf() || getFrontendCsrf() || "",
        },
        headers || {}
      );
      const resp = await fetch(url, {
        method,
        headers: finalHeaders,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
        credentials: "same-origin",
      });
      const status = resp.status;
      let data = null;
      try {
        data = await resp.json();
      } catch {
        data = null;
      }
      return { ok: resp.ok, status, data };
    } catch (err) {
      if (err && err.name === "AbortError") {
        return { ok: false, status: 0, data: null };
      }
      console.error("AI Chat: fetchJSON error", err);
      return { ok: false, status: 0, data: null };
    } finally {
      clearTimeout(t);
    }
  }

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
  icon.src = "/website_ai_chat_min/static/description/icon.png";
  icon.alt = "AI";
  icon.className = "ai-chat-min__bubble-icon";
  bubble.appendChild(icon);

  const bubbleLabel = document.createElement("span");
  bubbleLabel.className = "ai-chat-min__bubble-label";
  bubbleLabel.textContent = "AI";
  bubble.appendChild(bubbleLabel);

  // Panel (chat window)
  const panel = document.createElement("section");
  panel.className = "ai-chat-min__panel";
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
  input.className = "ai-chat-min__input";
  input.placeholder = "Ask Academy AI anything about your courses...";
  input.setAttribute("autocomplete", "off");
  const send = document.createElement("button");
  send.type = "button";
  send.className = "ai-chat-min__send";
  send.textContent = "Send";

  footer.appendChild(input);
  footer.appendChild(send);

  panel.appendChild(header);
  panel.appendChild(modelsBar);
  panel.appendChild(body);
  panel.appendChild(footer);

  wrap.appendChild(bubble);
  wrap.appendChild(panel);

  document.body.appendChild(wrap);

  // ---- MESSAGE RENDERING ----
  function appendMessage(role, text) {
    const msg = document.createElement("div");
    msg.className = `ai-chat-min__msg ai-chat-min__msg--${role}`;
    const bubble = document.createElement("div");
    bubble.className = "ai-chat-min__msg-bubble";
    bubble.textContent = text;
    msg.appendChild(bubble);
    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }

  // Basic markdown-ish rendering for bot UI
  function renderMarkdownToElement(md, el) {
    const text = String(md || "");
    const hasFormatting = /[#*_`]/.test(text) || text.includes("\n");
    if (!hasFormatting) {
      el.textContent = text;
      return;
    }

    const lines = text.split(/\r?\n/);
    let buf = [];
    const flushParagraph = () => {
      if (!buf.length) return;
      const p = document.createElement("p");
      p.textContent = buf.join(" ");
      el.appendChild(p);
      buf = [];
    };

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        flushParagraph();
        continue;
      }
      if (/^#{1,6}\s+/.test(trimmed)) {
        flushParagraph();
        const level = Math.min(trimmed.match(/^#+/)[0].length, 4);
        const h = document.createElement(level === 1 ? "h3" : level === 2 ? "h4" : "h5");
        h.textContent = trimmed.replace(/^#{1,6}\s+/, "");
        el.appendChild(h);
        continue;
      }
      if (/^[-*]\s+/.test(trimmed)) {
        flushParagraph();
        const ul = document.createElement("ul");
        const li = document.createElement("li");
        li.textContent = trimmed.replace(/^[-*]\s+/, "");
        ul.appendChild(li);
        el.appendChild(ul);
        continue;
      }
      buf.push(trimmed);
    }
    flushParagraph();
  }

  function appendBotUI(ui) {
    const msg = document.createElement("div");
    msg.className = "ai-chat-min__msg ai-chat-min__msg--bot";

    const bubble = document.createElement("div");
    bubble.className = "ai-chat-min__msg-bubble ai-chat-min__msg-bubble--rich";

    if (ui.title) {
      const h = document.createElement("h3");
      h.className = "waicm-title";
      h.textContent = ui.title;
      bubble.appendChild(h);
    }

    if (ui.summary) {
      const s = document.createElement("p");
      s.className = "waicm-summary";
      s.textContent = ui.summary;
      bubble.appendChild(s);
    }

    const answer = document.createElement("div");
    answer.className = "waicm-answer";
    renderMarkdownToElement(ui.answer_md || "", answer);
    bubble.appendChild(answer);

    if (Array.isArray(ui.suggestions) && ui.suggestions.length) {
      const sugWrap = document.createElement("div");
      sugWrap.className = "waicm-suggestions";
      for (const s of ui.suggestions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "waicm-suggest";
        btn.textContent = s;
        btn.addEventListener("click", () => {
          input.value = s;
          input.focus();
        });
        sugWrap.appendChild(btn);
      }
      bubble.appendChild(sugWrap);
    }

    msg.appendChild(bubble);
    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }

  // ---- MODEL USAGE HELPERS ----
  function resetModelUsage(models) {
    for (const key of Object.keys(modelUsage)) {
      delete modelUsage[key];
    }
    if (Array.isArray(models)) {
      for (const m of models) {
        if (!m) continue;
        const code = m.model_name;
        if (!code) continue;
        const limit =
          typeof m.prompt_limit === "number" ? m.prompt_limit : null;
        modelUsage[code] = {
          prompt_limit: limit,
          prompts_used: 0,
        };
      }
    }
  }

  function updatePromptCounterUI() {
    if (!modelLimitLabelEl) return;
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
    if (!selectedModelName || !modelUsage[selectedModelName]) return;
    const meta = modelUsage[selectedModelName];
    if (!meta.prompt_limit || meta.prompt_limit <= 0) {
      // unlimited – nothing meaningful to display
      return;
    }
    meta.prompts_used = (meta.prompts_used || 0) + 1;
    updatePromptCounterUI();
  }

  function renderModels(models, defaultModel) {
    modelsBar.innerHTML = "";
    modelSelectEl = null;
    modelLimitLabelEl = null;

    if (!Array.isArray(models) || !models.length) {
      modelsBar.style.display = "none";
      selectedModelName = null;
      // Still keep a shared chat history even if models can't be listed
      loadChatState();
      return;
    }

    modelsBar.style.display = "flex";

    resetModelUsage(models);

    // Choose default: backend default or first model
    selectedModelName =
      defaultModel || (models[0] && models[0].model_name) || null;

    // Label
    const labelEl = document.createElement("span");
    labelEl.className = "ai-chat-min__model-label";
    labelEl.textContent = "Model";

    // Select
    const selectEl = document.createElement("select");
    selectEl.className = "ai-chat-min__model-select";

    for (const m of models) {
      if (!m) continue;
      const code = m.model_name;
      if (!code) continue;
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = code;
      selectEl.appendChild(opt);
    }

    if (selectedModelName) {
      selectEl.value = selectedModelName;
    }

    // Dynamic prompt counter (left = model, right = usage)
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

    // Load a single shared chat history (independent of model)
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
    const isMax = panel.classList.toggle("ai-chat-min__panel--max");
    maxBtn.textContent = isMax ? "🗗" : "🗖";
    if (isMax) {
      maxBtn.setAttribute("aria-label", "Restore");
      maxBtn.title = "Restore";
    } else {
      maxBtn.setAttribute("aria-label", "Maximize");
      maxBtn.title = "Maximize";
    }
  });

  // ---- New chat / Reset ----
  resetBtn.addEventListener("click", () => {
    const confirmed = window.confirm(
      "Start a new chat? This will clear the conversation in this browser."
    );
    if (!confirmed) return;
    resetChat();
  });

  // ---- SEND FLOW ----
  async function sendMsg() {
    const rawText = input.value;
    const text = String(rawText || "").trim();
    if (!text) return;
    if (!selectedModelName) {
      window.alert("No AI model is available for your account.");
      return;
    }

    input.value = "";
    appendMessage("user", text);
    pushUserMessageToState(text);

    const prompt = buildPromptWithHistory(text);

    send.disabled = true;
    appendMessage("bot", "Thinking...");

    try {
      const payload = {
        jsonrpc: "2.0",
        method: "call",
        params: {
          question: prompt,
          model_name: selectedModelName,
        },
      };

      const { ok, status, data } = await fetchJSON("/ai_chat/send", {
        method: "POST",
        body: payload,
        timeoutMs: 60000,
      });

      // Remove the "Thinking..." placeholder
      const last = body.lastElementChild;
      if (last && last.classList.contains("ai-chat-min__msg--bot")) {
        body.removeChild(last);
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
        pushModelMessageToState(ui.answer_md);
        // Front-end dynamic counter (backend remains authoritative for daily quota)
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
    if (!mount) {
      return;
    }
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
  })();
})();
