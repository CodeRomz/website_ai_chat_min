(function () {
  function getCookie(name) {
    const m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return m ? decodeURIComponent(m[2]) : '';
  }
  async function rpc(route, params) {
    const payload = { jsonrpc: '2.0', method: 'call', params: params || {}, id: Math.floor(Math.random()*1e9) };
    const r = await fetch(route, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'X-Openerp-CSRF-Token': getCookie('csrf_token') || getCookie('CSRF_TOKEN') || '',
      },
      body: JSON.stringify(payload),
      credentials: 'include',
    });
    const j = await r.json();
    if (j.error) throw new Error(j.error.data ? j.error.data.message : 'Server error');
    return j.result;
  }

  function mountUI() {
    let root = document.getElementById('ai-chat-min-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'ai-chat-min-root';
      document.body.appendChild(root);
    }
    root.innerHTML = ''
      + '<div class="ai-fab" id="ai_fab">💬</div>'
      + '<div class="ai-panel hidden" id="ai_panel">'
      + '  <div class="ai-header">AI Assistant</div>'
      + '  <div class="ai-msgs" id="ai_msgs"></div>'
      + '  <div class="ai-input">'
      + '    <input id="ai_text" type="text" placeholder="Type a message…" maxlength="2000">'
      + '    <button id="ai_send">Send</button>'
      + '  </div>'
      + '</div>';

    const fab = document.getElementById('ai_fab');
    const panel = document.getElementById('ai_panel');
    const msgs = document.getElementById('ai_msgs');
    const text = document.getElementById('ai_text');
    const send = document.getElementById('ai_send');

    function add(role, t) {
      const el = document.createElement('div');
      el.className = 'msg ' + role;
      el.textContent = t;
      msgs.appendChild(el);
      msgs.scrollTop = msgs.scrollHeight;
    }

    fab.addEventListener('click', function () {
      panel.classList.toggle('hidden');
      if (!panel.classList.contains('hidden')) text.focus();
    });

    async function doSend() {
      const v = (text.value || '').trim();
      if (!v) return;
      add('user', v);
      text.value = '';
      add('bot', '…');
      try {
        const res = await rpc('/ai_chat/send', { message: v });
        if (msgs.lastChild && msgs.lastChild.textContent === '…') msgs.removeChild(msgs.lastChild);
        if (!res || !res.ok) add('bot', 'Error: ' + (res && res.error || 'unknown'));
        else add('bot', res.reply || '(no reply)');
      } catch (e) {
        if (msgs.lastChild && msgs.lastChild.textContent === '…') msgs.removeChild(msgs.lastChild);
        add('bot', 'Network error.');
        console.error(e);
      }
    }

    send.addEventListener('click', doSend);
    text.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') doSend(); });
  }

  async function init() {
    try {
      const r = await rpc('/ai_chat/can_load', {});
      if (!r || !r.ok || !r.show) return; // not logged in or not in group
      mountUI();
    } catch (e) {
      // public user: ignore
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
