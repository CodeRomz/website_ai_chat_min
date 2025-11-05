// This file is loaded as <script type="module">, so top-level await is allowed.

async function buildRpc() {
  try {
    // Preferred: Odoo 17 service (handles CSRF, errors)
    const mod = await import("@web/core/network/rpc_service");
    return mod.jsonRpc;
  } catch (e) {
    // Fallback: minimal JSON-RPC over fetch with CSRF header
    const getCookie = (name) => {
      const m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
      return m ? decodeURIComponent(m[2]) : "";
    };
    const csrf = getCookie("csrf_token") || getCookie("CSRF_TOKEN") || "";
    return async function (route, _method, params) {
      const payload = {
        jsonrpc: "2.0",
        method: "call",
        params: params || {},
        id: Math.floor(Math.random() * 1e9),
      };
      const r = await fetch(route, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest",
          "X-Openerp-CSRF-Token": csrf, // accepted by csrf=True JSON routes
        },
        body: JSON.stringify(payload),
        credentials: "include",
      });
      const j = await r.json();
      if (j.error) throw new Error(j.error.data ? j.error.data.message : "Server error");
      return j.result;
    };
  }
}

const rpc = await buildRpc();

(function mount() {
  const root = document.getElementById("ai-chat-min-root");
  if (!root) return;

  root.innerHTML = \`
    <div class="ai-fab" id="ai_fab">💬</div>
    <div class="ai-panel hidden" id="ai_panel">
      <div class="ai-header">AI Assistant</div>
      <div class="ai-msgs" id="ai_msgs"></div>
      <div class="ai-input">
        <input id="ai_text" type="text" placeholder="Type a message…" maxlength="2000">
        <button id="ai_send">Send</button>
      </div>
    </div>
  \`;

  const $fab = document.getElementById("ai_fab");
  const $panel = document.getElementById("ai_panel");
  const $msgs = document.getElementById("ai_msgs");
  const $text = document.getElementById("ai_text");
  const $send = document.getElementById("ai_send");

  function add(role, text) {
    const el = document.createElement("div");
    el.className = \`msg \${role}\`;
    el.textContent = text;
    $msgs.appendChild(el);
    $msgs.scrollTop = $msgs.scrollHeight;
  }

  $fab.addEventListener("click", () => {
    $panel.classList.toggle("hidden");
    if (!$panel.classList.contains("hidden")) $text.focus();
  });

  async function send() {
    const v = ($text.value || "").trim();
    if (!v) return;
    add("user", v);
    $text.value = "";
    add("bot", "…");

    try {
      const r = await rpc("/ai_chat/send", "call", { message: v });
      // remove typing indicator
      if ($msgs.lastChild && $msgs.lastChild.textContent === "…") {
        $msgs.removeChild($msgs.lastChild);
      }
      if (!r || !r.ok) {
        add("bot", \`Error: \${(r && r.error) || "unknown"}\`);
      } else {
        add("bot", r.reply || "(no reply)");
      }
    } catch (e) {
      if ($msgs.lastChild && $msgs.lastChild.textContent === "…") {
        $msgs.removeChild($msgs.lastChild);
      }
      add("bot", "Network error.");
      console.error(e);
    }
  }

  $send.addEventListener("click", send);
  $text.addEventListener("keydown", (ev) => { if (ev.key === "Enter") send(); });
})();
