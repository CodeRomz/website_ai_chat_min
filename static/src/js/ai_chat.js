(function () {
    "use strict";

    function getCSRFToken() {
        try {
            const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
            return m ? decodeURIComponent(m[1]) : "";
        } catch (e) {
            return "";
        }
    }

    async function postJSON(url, payload, withCSRF) {
        const headers = {"Content-Type": "application/json"};
        if (withCSRF) {
            headers["X-Openerp-CSRF-Token"] = getCSRFToken();
        }
        const res = await fetch(url, {
            method: "POST",
            headers: headers,
            body: JSON.stringify(payload || {}),
            credentials: "include"
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return await res.json();
    }

    function mountUI() {
        if (document.getElementById("ai-chat-min")) return;

        const root = document.createElement("div");
        root.id = "ai-chat-min";
        root.innerHTML = `
            <div class="ai-chat-min__bubble" title="AI Chat">💬</div>
            <div class="ai-chat-min__panel">
                <div class="ai-chat-min__header">AI Chat</div>
                <div class="ai-chat-min__body"></div>
                <div class="ai-chat-min__footer">
                    <input type="text" class="ai-chat-min__input" placeholder="Ask a question..." />
                    <button class="ai-chat-min__send">Send</button>
                </div>
            </div>
        `;
        document.body.appendChild(root);

        const bubble = root.querySelector(".ai-chat-min__bubble");
        const panel = root.querySelector(".ai-chat-min__panel");
        const body = root.querySelector(".ai-chat-min__body");
        const input = root.querySelector(".ai-chat-min__input");
        const send = root.querySelector(".ai-chat-min__send");

        bubble.addEventListener("click", () => {
            panel.classList.toggle("open");
            if (panel.classList.contains("open")) {
                input.focus();
            }
        });

        async function sendMessage() {
            const msg = (input.value || "").trim();
            if (!msg) return;
            input.value = "";
            body.insertAdjacentHTML("beforeend", `<div class="ai-chat-min__msg ai-chat-min__msg--me"></div>`);
            body.lastElementChild.textContent = msg;
            body.insertAdjacentHTML("beforeend", `<div class="ai-chat-min__msg ai-chat-min__msg--bot">...</div>`);
            try {
                const res = await postJSON("/ai_chat/send", {message: msg}, true);
                if (res && res.ok) {
                    body.lastElementChild.textContent = res.reply || "(no reply)";
                } else {
                    body.lastElementChild.textContent = "Error: " + (res && res.error ? res.error : "Unknown error");
                }
            } catch (e) {
                body.lastElementChild.textContent = "Network error.";
            }
            body.scrollTop = body.scrollHeight;
        }

        send.addEventListener("click", sendMessage);
        input.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter") {
                ev.preventDefault();
                sendMessage();
            }
        });
    }

    (async function init() {
        try {
            const res = await postJSON("/ai_chat/can_load", {}, false);
            if (res && res.show) {
                mountUI();
            }
        } catch (e) {
            // Ignore silently
        }
    })();
})();
