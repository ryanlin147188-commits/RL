/**
 * Agent 浮動聊天框 — Phase 1c-3。
 *
 * IIFE self-mount:不需要 index.html 加 markup,只需 import 這個檔。
 * 走 window.fetch(已 monkey-patch 自動帶 Bearer + 401 silent refresh)。
 *
 * UI 層:
 *   - FAB(右下圓鈕)收合時 56x56 / 展開後 380x600 卡片
 *   - 卡片:Header(session 標題 + model picker + 關閉)
 *           → Body(訊息流;virtualization 暫不做,訊息 < 200 條時純 append)
 *           → Footer(textarea + 送出 + 本 session 總成本)
 *   - Modal:Confirm dialog(tool message 含 pending_action_id 時觸發)
 *
 * Endpoints(對應 backend Phase 0~1c-2):
 *   GET   /api/agent/sessions
 *   POST  /api/agent/sessions
 *   GET   /api/agent/sessions/{id}/messages
 *   POST  /api/agent/sessions/{id}/messages
 *   POST  /api/agent/sessions/{id}/pending-actions/{aid}/approve
 *   POST  /api/agent/sessions/{id}/pending-actions/{aid}/reject
 *
 * 不在這版做(留下一輪):
 *   - 訂閱 /ws/v1/executions/{task_id}/logs(tool 完成事件即時推播)
 *   - 拖曳 panel
 *   - markdown / 程式碼高亮(content 純文字 render,避免 XSS)
 *   - 多 session 切換 UI(只支援當前一個 active session;切換功能下一輪)
 */
(function () {
    "use strict";

    // ── 三家代表 model(後端 router.infer_provider 認得的前綴) ──────────
    // 沒在這清單但前綴對的 model id user 也能用,只是要透過自訂輸入。
    const DEFAULT_MODELS = [
        { id: "claude-opus-4-7", label: "Claude Opus 4.7 (最強)" },
        { id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6 (平衡)" },
        { id: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5 (快速)" },
        { id: "gpt-4o", label: "OpenAI GPT-4o" },
        { id: "gpt-4o-mini", label: "OpenAI GPT-4o mini (便宜)" },
        { id: "gemini-2.5-pro", label: "Google Gemini 2.5 Pro" },
        { id: "gemini-2.5-flash", label: "Google Gemini 2.5 Flash" },
    ];

    const state = {
        open: false,
        sessionId: null,
        sessionTitle: null,
        sessionModel: null,
        messages: [],          // 完整 history,順序 by seq
        sending: false,        // chat 進行中,擋雙擊
        totalCostUsd: 0,       // session 累計成本
        // pending_action_id → action info(預填從 message 解出來)
        // 用來查「這條 message 對應的 pending 還活著嗎」
        pendingActions: new Map(),
        // task_id → WebSocket(訂閱 /ws/v1/executions/{tid}/logs)
        // 看到 tool message 有 task_id 自動連;收到 completed 事件就 refresh
        taskSockets: new Map(),
        // task_id → 最新進度文字(passed/failed 計數),render 時顯示在 badge 旁
        taskProgress: new Map(),
    };

    // ── DOM 工具 ──────────────────────────────────────────────────────

    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === "class") node.className = v;
            else if (k === "html") node.innerHTML = v;
            else if (k.startsWith("on") && typeof v === "function") {
                node.addEventListener(k.slice(2).toLowerCase(), v);
            } else if (v !== null && v !== undefined) {
                node.setAttribute(k, v);
            }
        }
        for (const c of [].concat(children)) {
            if (c === null || c === undefined) continue;
            node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        }
        return node;
    }

    function escapeText(s) {
        // 純文字 render,絕不 innerHTML(防 LLM 回應 / tool result 帶 XSS)
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    }

    // ── API ──────────────────────────────────────────────────────────

    async function api(path, opts = {}) {
        const resp = await fetch(path, {
            ...opts,
            headers: {
                "Content-Type": "application/json",
                ...(opts.headers || {}),
            },
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try {
                const j = await resp.json();
                detail = j.detail || JSON.stringify(j);
            } catch (_) { /* not json */ }
            throw new Error(detail);
        }
        if (resp.status === 204) return null;
        return resp.json();
    }

    // ── Refs(DOM 節點 — 建構時填入) ──────────────────────────────

    const refs = {
        fab: null,
        panel: null,
        header: null,
        body: null,
        footer: null,
        textarea: null,
        sendBtn: null,
        sessionLabel: null,
        modelPicker: null,
        costLabel: null,
        confirmBackdrop: null,
        confirmDialog: null,
        sidebar: null,
        sidebarBody: null,
    };

    // ── Build UI ──────────────────────────────────────────────────────

    function buildFab() {
        // 不依賴 tailwind 預編譯(那份 tailwind.css 缺 bottom-6 / w-14 / z-[150]
        // 等 utility class — 用 inline style 寫死關鍵 positioning 與大小,
        // 確保 FAB 真的在 viewport 右下角,不會退化跑到頁面其他位置。
        const fab = el("button", {
            type: "button",
            "aria-label": "開啟 AI 助手",
            class: "agent-chat-fab",
            style:
                "position:fixed; bottom:24px; right:24px; z-index:2147483640;" +
                " width:56px; height:56px; border-radius:9999px; border:none;" +
                " color:#fff; font-size:22px; cursor:pointer;" +
                " background:linear-gradient(135deg,#f59e0b,#f97316);" +
                " box-shadow:0 10px 25px rgba(0,0,0,.25);" +
                " display:flex; align-items:center; justify-content:center;" +
                " transition:transform .15s ease;",
            onclick: togglePanel,
        }, [el("i", { class: "fa-solid fa-wand-magic-sparkles" })]);
        // hover / active 縮放 — 也走 inline 避免 tailwind 缺 class
        fab.addEventListener("mouseenter", () => { fab.style.transform = "scale(1.05)"; });
        fab.addEventListener("mouseleave", () => { fab.style.transform = "scale(1)"; });
        refs.fab = fab;
        document.body.appendChild(fab);
    }

    function buildPanel() {
        const panel = el("div", {
            id: "agentChatPanel",
            class:
                "agent-chat-panel hidden bg-white rounded-2xl shadow-2xl " +
                "border border-stone-200 flex flex-col overflow-hidden",
            // 關鍵 positioning + 尺寸 inline,不依賴 tailwind 預編譯
            style:
                "position:fixed; bottom:24px; right:24px; z-index:2147483641;" +
                " width:380px; height:600px; max-width:calc(100vw - 48px);" +
                " max-height:calc(100vh - 48px);",
            role: "dialog",
            "aria-label": "AI 助手對話",
        });

        // Header
        const sessionLabel = el("div", {
            class: "flex-1 truncate text-sm font-semibold text-stone-700",
        }, "AI 助手");
        const modelPicker = el("select", {
            class:
                "text-[11px] bg-stone-100 border border-stone-200 rounded " +
                "px-1.5 py-0.5 max-w-[140px]",
            title: "選擇 LLM 模型",
            onchange: onModelChange,
        }, DEFAULT_MODELS.map(m =>
            el("option", { value: m.id }, m.label)
        ));
        const historyBtn = el("button", {
            type: "button",
            class: "text-stone-400 hover:text-amber-500 p-1",
            title: "歷史對話",
            onclick: toggleSessionSidebar,
        }, [el("i", { class: "fa-solid fa-clock-rotate-left" })]);
        const newBtn = el("button", {
            type: "button",
            class: "text-stone-400 hover:text-amber-500 p-1",
            title: "新對話",
            onclick: startNewSession,
        }, [el("i", { class: "fa-solid fa-plus" })]);
        const closeBtn = el("button", {
            type: "button",
            class: "text-stone-400 hover:text-rose-500 p-1",
            title: "收合",
            onclick: togglePanel,
        }, [el("i", { class: "fa-solid fa-xmark" })]);
        const header = el("div", {
            class:
                "flex items-center gap-2 px-3 py-2 border-b border-stone-200 " +
                "bg-stone-50",
        }, [
            el("i", { class: "fa-solid fa-robot text-amber-500" }),
            sessionLabel,
            modelPicker,
            historyBtn,
            newBtn,
            closeBtn,
        ]);
        refs.sessionLabel = sessionLabel;
        refs.modelPicker = modelPicker;

        // Body(訊息列表)
        const body = el("div", {
            class:
                "flex-1 overflow-y-auto px-3 py-3 space-y-2 text-sm " +
                "bg-stone-50/50",
        });
        refs.body = body;

        // Footer(輸入)
        const textarea = el("textarea", {
            rows: 2,
            placeholder: "輸入訊息,Enter 送出(Shift+Enter 換行)…",
            class:
                "flex-1 text-sm border border-stone-300 rounded px-2 py-1.5 " +
                "resize-none focus:outline-none focus:ring-2 focus:ring-amber-400",
        });
        textarea.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
            }
        });
        const sendBtn = el("button", {
            type: "button",
            class:
                "px-3 bg-amber-500 hover:bg-amber-600 text-white rounded " +
                "text-sm font-semibold disabled:opacity-50 disabled:cursor-not-allowed",
            onclick: handleSend,
        }, [el("i", { class: "fa-solid fa-paper-plane" })]);
        const costLabel = el("div", {
            class: "text-[10px] text-stone-400 mt-1 text-right",
        }, "本對話成本:$0.000000");
        const footer = el("div", {
            class: "border-t border-stone-200 bg-white p-2",
        }, [
            el("div", { class: "flex gap-2 items-stretch" }, [textarea, sendBtn]),
            costLabel,
        ]);
        refs.textarea = textarea;
        refs.sendBtn = sendBtn;
        refs.costLabel = costLabel;

        panel.appendChild(header);
        panel.appendChild(body);
        panel.appendChild(footer);
        refs.panel = panel;
        document.body.appendChild(panel);
    }

    function buildSessionSidebar() {
        // 從 panel 左側滑出的抽屜,蓋在 panel 內;點外面收回
        const sidebar = el("div", {
            id: "agentSessionSidebar",
            class:
                "agent-chat-sidebar bg-white border-r border-stone-200 " +
                "shadow-xl flex flex-col",
            style:
                "position:absolute; top:0; bottom:0; left:0; width:60%;" +
                " z-index:10; transform:translateX(-100%);" +
                " transition:transform .2s ease-out;",
        });
        const sidebarHeader = el("div", {
            class:
                "flex items-center gap-2 px-3 py-2 border-b border-stone-200 " +
                "bg-stone-50 text-sm font-semibold text-stone-700",
        }, [
            el("i", { class: "fa-solid fa-clock-rotate-left text-stone-400" }),
            el("span", {}, "歷史對話"),
            el("button", {
                type: "button",
                class: "ml-auto text-stone-400 hover:text-rose-500 p-1",
                title: "關閉",
                onclick: toggleSessionSidebar,
            }, [el("i", { class: "fa-solid fa-xmark text-xs" })]),
        ]);
        const sidebarBody = el("div", {
            class: "flex-1 overflow-y-auto",
        });
        sidebar.appendChild(sidebarHeader);
        sidebar.appendChild(sidebarBody);
        refs.sidebar = sidebar;
        refs.sidebarBody = sidebarBody;
        refs.panel.appendChild(sidebar);
    }

    async function toggleSessionSidebar() {
        // 用 inline style 而非 Tailwind class(該 class 在預編譯 tailwind.css 內缺)
        const wasHidden = refs.sidebar.style.transform.includes("-100%");
        if (wasHidden) {
            await refreshSessionsList();
            refs.sidebar.style.transform = "translateX(0)";
        } else {
            refs.sidebar.style.transform = "translateX(-100%)";
        }
    }

    async function refreshSessionsList() {
        try {
            const sessions = await api("/api/agent/sessions?limit=50");
            refs.sidebarBody.replaceChildren();
            if (!sessions || sessions.length === 0) {
                refs.sidebarBody.appendChild(
                    el("div", { class: "text-xs text-stone-400 italic p-3" },
                        "尚無歷史對話")
                );
                return;
            }
            for (const s of sessions) {
                const isCurrent = s.id === state.sessionId;
                const row = el("div", {
                    class:
                        "px-3 py-2 border-b border-stone-100 cursor-pointer " +
                        "hover:bg-amber-50 " +
                        (isCurrent ? "bg-amber-50" : ""),
                    onclick: () => switchToSession(s.id),
                }, [
                    el("div", {
                        class: "text-xs font-semibold text-stone-700 truncate",
                    }, s.title || "未命名對話"),
                    el("div", {
                        class: "text-[10px] text-stone-400",
                    }, [
                        s.model || "預設模型",
                        " · ",
                        formatRelativeTime(s.updated_at),
                    ].join("")),
                ]);
                refs.sidebarBody.appendChild(row);
            }
        } catch (e) {
            refs.sidebarBody.replaceChildren(
                el("div", { class: "text-xs text-rose-500 p-3" },
                    `載入失敗:${e.message}`)
            );
        }
    }

    function formatRelativeTime(iso) {
        if (!iso) return "";
        const t = new Date(iso).getTime();
        const now = Date.now();
        const sec = Math.floor((now - t) / 1000);
        if (sec < 60) return `${sec} 秒前`;
        if (sec < 3600) return `${Math.floor(sec / 60)} 分鐘前`;
        if (sec < 86400) return `${Math.floor(sec / 3600)} 小時前`;
        return `${Math.floor(sec / 86400)} 天前`;
    }

    async function switchToSession(sessionId) {
        if (state.sending) return;
        if (sessionId === state.sessionId) {
            await toggleSessionSidebar();
            return;
        }
        // 關掉所有舊 WS 連線(syncTaskSockets 會自己重連 active 的)
        for (const ws of state.taskSockets.values()) {
            try { ws.close(); } catch (_) { /* */ }
        }
        state.taskSockets.clear();
        state.taskProgress.clear();

        try {
            const session = await api(
                `/api/agent/sessions/${encodeURIComponent(sessionId)}`
            );
            state.sessionId = session.id;
            state.sessionTitle = session.title || "對話";
            state.sessionModel = session.model;
            if (state.sessionModel) {
                refs.modelPicker.value = state.sessionModel;
            }
            await refreshMessages();
            await toggleSessionSidebar();
        } catch (e) {
            renderSystemMessage(`切換失敗:${e.message}`, "rose");
        }
    }

    function buildConfirmModal() {
        const dialog = el("div", {
            class:
                "bg-white rounded-xl shadow-2xl w-[360px] max-w-[90vw] p-5 " +
                "border border-stone-200",
        });
        const backdrop = el("div", {
            class: "agent-chat-confirm-backdrop",
            style:
                "position:fixed; top:0; right:0; bottom:0; left:0;" +
                " z-index:2147483646; background:rgba(0,0,0,.45);" +
                " display:none; align-items:center; justify-content:center;" +
                " padding:16px;",
            role: "dialog",
            "aria-label": "二次確認",
        }, [dialog]);
        refs.confirmDialog = dialog;
        refs.confirmBackdrop = backdrop;
        document.body.appendChild(backdrop);
    }

    // ── 動作:開關 / 切換 model / 切換 session ──────────────────────

    function togglePanel() {
        state.open = !state.open;
        if (state.open) {
            refs.panel.classList.remove("hidden");
            refs.fab.classList.add("hidden");
            ensureSessionThenRefresh();
        } else {
            refs.panel.classList.add("hidden");
            refs.fab.classList.remove("hidden");
        }
    }

    function onModelChange() {
        state.sessionModel = refs.modelPicker.value;
        // model 改了就強制新開 session(沿用設計:per-session model 不在中途切)
        // 但只有 user 已有 session 時才提示
        if (state.sessionId) {
            startNewSession();
        }
    }

    async function startNewSession() {
        try {
            state.sending = true;
            updateUiBusy();
            const model = refs.modelPicker.value || null;
            const session = await api("/api/agent/sessions", {
                method: "POST",
                body: JSON.stringify({ model }),
            });
            state.sessionId = session.id;
            state.sessionTitle = session.title || "新對話";
            state.sessionModel = session.model;
            state.messages = [];
            state.totalCostUsd = 0;
            state.pendingActions.clear();
            renderAll();
        } catch (e) {
            renderSystemMessage(`新對話建立失敗:${e.message}`, "rose");
        } finally {
            state.sending = false;
            updateUiBusy();
        }
    }

    // ── 主流程 ────────────────────────────────────────────────────

    async function ensureSessionThenRefresh() {
        if (state.sessionId) {
            await refreshMessages();
            return;
        }
        // 首次開啟:撈最近一個 session,沒有就建新
        try {
            const sessions = await api("/api/agent/sessions?limit=1");
            if (sessions && sessions.length > 0) {
                state.sessionId = sessions[0].id;
                state.sessionTitle = sessions[0].title || "對話";
                state.sessionModel = sessions[0].model;
                if (state.sessionModel) {
                    refs.modelPicker.value = state.sessionModel;
                }
                await refreshMessages();
            } else {
                await startNewSession();
            }
        } catch (e) {
            renderSystemMessage(`載入對話失敗:${e.message}`, "rose");
        }
    }

    async function refreshMessages() {
        if (!state.sessionId) return;
        try {
            const msgs = await api(
                `/api/agent/sessions/${encodeURIComponent(state.sessionId)}/messages?limit=200`
            );
            state.messages = msgs || [];
            recomputeCost();
            renderAll();
            // 掃描新出現的 task_id,連 WS 訂閱進度
            syncTaskSockets();
        } catch (e) {
            renderSystemMessage(`載入訊息失敗:${e.message}`, "rose");
        }
    }

    // ── WebSocket:訂閱既有 executions log channel ────────────────────

    function syncTaskSockets() {
        // 1. 找出當前 messages 含的 task_ids
        const activeIds = new Set();
        for (const m of state.messages) {
            if (m.task_id && m.role === "tool") {
                // 已 rejected 的就不再連
                let parsed = null;
                try { parsed = JSON.parse(m.content || ""); } catch (_) { /* */ }
                const status = parsed && parsed.status;
                if (status === "user_rejected") continue;
                activeIds.add(m.task_id);
            }
        }
        // 2. 對沒連過的 task_id 開 WS
        for (const tid of activeIds) {
            if (!state.taskSockets.has(tid)) {
                connectTaskSocket(tid);
            }
        }
        // 3. 關掉 messages 已不再參考的 socket(session 切換 / message 被刪)
        for (const [tid, ws] of state.taskSockets.entries()) {
            if (!activeIds.has(tid)) {
                try { ws.close(); } catch (_) { /* */ }
                state.taskSockets.delete(tid);
                state.taskProgress.delete(tid);
            }
        }
    }

    function connectTaskSocket(taskId) {
        // 既有後端的 WS endpoint(executions.ws_router):/ws/v1/executions/{tid}/logs
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${location.host}/ws/v1/executions/${encodeURIComponent(taskId)}/logs`;
        let ws;
        try {
            ws = new WebSocket(url);
        } catch (_) {
            return;
        }
        ws.addEventListener("message", (ev) => {
            // 既有 backend 用 send_json 發 {type, data, ...} 的格式;對 agent 來說我們
            // 只在意「完成」事件 — 收到任何含 "completed"/"PASSED"/"FAILED" 的訊息
            // 就觸發一次 refreshMessages,讓後端 tool message 的最新狀態 push 上來。
            // 中途的 log 訊息也順便存進 taskProgress 給 UI 顯示「X passed / Y failed」。
            let payload = ev.data;
            try { payload = JSON.parse(ev.data); } catch (_) { /* keep string */ }
            updateTaskProgress(taskId, payload);
            if (isTaskTerminalEvent(payload)) {
                refreshMessages();
            }
        });
        ws.addEventListener("close", () => {
            state.taskSockets.delete(taskId);
        });
        ws.addEventListener("error", () => {
            // 後端 WS 連不上 — 不擋使用者,沿用 fail-open 哲學;只在 console 看得到
            console.warn("[agent-chat] task WS failed:", taskId);
        });
        state.taskSockets.set(taskId, ws);
    }

    function isTaskTerminalEvent(payload) {
        if (!payload) return false;
        const s = JSON.stringify(payload).toUpperCase();
        return (
            s.includes("\"COMPLETED\"") ||
            s.includes("\"STATUS\":\"PASSED\"") ||
            s.includes("\"STATUS\":\"FAILED\"") ||
            s.includes("\"DONE\"")
        );
    }

    function updateTaskProgress(taskId, payload) {
        // 後端可能送 {type:"progress", passed:X, failed:Y, total:Z} 之類
        if (!payload || typeof payload !== "object") return;
        const pieces = [];
        if (typeof payload.passed === "number") pieces.push(`✓ ${payload.passed}`);
        if (typeof payload.failed === "number") pieces.push(`✗ ${payload.failed}`);
        if (typeof payload.total === "number") pieces.push(`/ ${payload.total}`);
        if (pieces.length > 0) {
            state.taskProgress.set(taskId, pieces.join(" "));
            // 不整段 refresh,只更新 badge 文字
            renderAll();
        }
    }

    async function handleSend() {
        if (state.sending) return;
        const text = (refs.textarea.value || "").trim();
        if (!text) return;
        if (!state.sessionId) await startNewSession();
        if (!state.sessionId) return; // 建 session 失敗

        state.sending = true;
        updateUiBusy();
        // 樂觀 render:先把 user message append
        const optimistic = {
            id: `__optimistic_${Date.now()}`,
            session_id: state.sessionId,
            role: "user",
            content: text,
            seq: (state.messages[state.messages.length - 1]?.seq || 0) + 1,
            created_at: new Date().toISOString(),
            __optimistic: true,
        };
        state.messages.push(optimistic);
        renderAll();
        refs.textarea.value = "";

        try {
            const resp = await api(
                `/api/agent/sessions/${encodeURIComponent(state.sessionId)}/messages`,
                {
                    method: "POST",
                    body: JSON.stringify({ content: text }),
                }
            );
            // backend 回 user_message + assistant_message;refresh 拿完整 history
            // (有可能中間還有 tool_use / tool_result)
            await refreshMessages();
        } catch (e) {
            // 拿掉 optimistic
            state.messages = state.messages.filter(m => !m.__optimistic);
            renderAll();
            renderSystemMessage(`送出失敗:${e.message}`, "rose");
        } finally {
            state.sending = false;
            updateUiBusy();
        }
    }

    // ── 二次確認 modal ──────────────────────────────────────────

    function openConfirmModal(msg) {
        const actionId = msg.pending_action_id;
        if (!actionId) return;

        // 解析 placeholder JSON 拿 tool 名稱 / arguments
        let info = { tool_name: "未知", arguments: {}, expires_at: null };
        try {
            const parsed = JSON.parse(msg.content || "{}");
            info.tool_name = parsed.tool_name || info.tool_name;
            info.arguments = parsed.arguments || {};
            info.expires_at = parsed.expires_at || null;
        } catch (_) { /* ignore */ }

        const argsList = el("ul", { class: "text-xs text-stone-600 mt-2 space-y-1" });
        for (const [k, v] of Object.entries(info.arguments).slice(0, 8)) {
            argsList.appendChild(el("li", {}, [
                el("span", { class: "font-semibold text-stone-700" }, k + ": "),
                document.createTextNode(JSON.stringify(v)),
            ]));
        }

        const onApprove = async () => {
            await resolvePending(actionId, "approve");
            closeConfirmModal();
        };
        const onReject = async () => {
            await resolvePending(actionId, "reject");
            closeConfirmModal();
        };

        refs.confirmBackdrop.style.display = "flex";
        refs.confirmDialog.replaceChildren(
            el("div", { class: "flex items-center gap-2 mb-2" }, [
                el("i", { class: "fa-solid fa-shield-halved text-amber-500 text-lg" }),
                el("h3", { class: "text-base font-bold text-stone-800" }, "需要確認"),
            ]),
            el("p", { class: "text-sm text-stone-600 leading-relaxed" },
                `AI 想要執行 `,
            ),
            el("p", { class: "text-sm font-mono bg-stone-100 px-2 py-1 rounded my-2" },
                info.tool_name,
            ),
            argsList,
            info.expires_at
                ? el("p", { class: "text-[10px] text-stone-400 mt-2" },
                    `到期時間:${new Date(info.expires_at).toLocaleString()}`)
                : null,
            el("div", { class: "flex gap-2 mt-4" }, [
                el("button", {
                    type: "button",
                    class:
                        "flex-1 py-2 bg-rose-100 hover:bg-rose-200 text-rose-700 " +
                        "rounded text-sm font-semibold",
                    onclick: onReject,
                }, "拒絕"),
                el("button", {
                    type: "button",
                    class:
                        "flex-1 py-2 bg-amber-500 hover:bg-amber-600 text-white " +
                        "rounded text-sm font-semibold",
                    onclick: onApprove,
                }, "同意執行"),
            ]),
        );
        // 已在函式頂端設 display:flex,這裡 no-op(保留結構讓 git diff 清楚)
    }

    function closeConfirmModal() {
        refs.confirmBackdrop.style.display = "none";
    }

    async function resolvePending(actionId, mode) {
        if (state.sending) return;
        if (!state.sessionId) return;
        state.sending = true;
        updateUiBusy();
        try {
            const path =
                `/api/agent/sessions/${encodeURIComponent(state.sessionId)}` +
                `/pending-actions/${encodeURIComponent(actionId)}/${mode}`;
            await api(path, { method: "POST" });
            await refreshMessages();
        } catch (e) {
            renderSystemMessage(
                `${mode === "approve" ? "同意" : "拒絕"}失敗:${e.message}`,
                "rose"
            );
        } finally {
            state.sending = false;
            updateUiBusy();
        }
    }

    // ── Render ────────────────────────────────────────────────────

    function renderAll() {
        refs.sessionLabel.textContent = state.sessionTitle || "AI 助手";
        if (refs.body) refs.body.replaceChildren();
        for (const m of state.messages) {
            const node = renderMessage(m);
            if (node) refs.body.appendChild(node);
        }
        refs.body.scrollTop = refs.body.scrollHeight;
        renderCost();
    }

    function renderMessage(m) {
        switch (m.role) {
            case "user":
                return el("div", {
                    class: "flex justify-end",
                }, [
                    el("div", {
                        class:
                            "max-w-[80%] bg-amber-500 text-white px-3 py-1.5 " +
                            "rounded-lg rounded-tr-sm text-sm whitespace-pre-wrap " +
                            "break-words",
                    }, m.content || ""),
                ]);

            case "assistant":
                if (m.tool_calls && m.tool_calls.length > 0) {
                    // tool_use:灰底,顯示「要執行 X」
                    return el("div", {
                        class:
                            "flex flex-col gap-1 text-xs text-stone-500 " +
                            "italic px-2 py-1.5",
                    }, [
                        m.content
                            ? el("div", { class: "text-stone-700 not-italic mb-1" }, m.content)
                            : null,
                        ...m.tool_calls.map(tc =>
                            el("div", {
                                class:
                                    "bg-stone-100 border border-stone-200 rounded " +
                                    "px-2 py-1",
                            }, [
                                el("span", {}, "⚙ "),
                                el("span", { class: "font-mono text-stone-600" }, tc.name),
                                el("span", { class: "text-stone-400" },
                                    `(${Object.keys(tc.arguments || {}).length} args)`),
                            ])
                        ),
                    ]);
                }
                return renderAssistantText(m);

            case "tool":
                return renderToolResult(m);

            case "system":
                return el("div", {
                    class: "text-center text-xs text-stone-400 italic",
                }, m.content || "");

            default:
                return null;
        }
    }

    function renderAssistantText(m) {
        const wrap = el("div", { class: "flex justify-start" }, [
            el("div", {
                class:
                    "max-w-[85%] bg-white border border-stone-200 px-3 py-1.5 " +
                    "rounded-lg rounded-tl-sm text-sm text-stone-800 " +
                    "whitespace-pre-wrap break-words",
            }, m.content || ""),
        ]);
        // 成本標記(右下小字)
        if (m.usage && m.usage.cost_usd != null) {
            const cost = Number(m.usage.cost_usd);
            if (cost > 0) {
                wrap.appendChild(
                    el("div", {
                        class: "ml-2 self-end text-[10px] text-stone-400",
                        title:
                            `${m.usage.input_tokens} in / ${m.usage.output_tokens} out` +
                            (m.usage.cache_read_tokens
                                ? ` / ${m.usage.cache_read_tokens} cached`
                                : ""),
                    }, `$${cost.toFixed(6)}`)
                );
            }
        }
        return wrap;
    }

    function renderToolResult(m) {
        // pending placeholder?
        const isPending = !!m.pending_action_id;
        let parsed = null;
        try { parsed = JSON.parse(m.content || ""); } catch (_) { /* not json */ }

        const isAwaiting = parsed && parsed.status === "awaiting_user_confirmation";
        const isRejected = parsed && parsed.status === "user_rejected";

        // 摘要 line(永遠顯示)
        let badgeText = "tool result";
        let badgeColor = "bg-stone-100 text-stone-600";
        if (isAwaiting) {
            badgeText = "等待確認";
            badgeColor = "bg-amber-100 text-amber-700";
        } else if (isRejected) {
            badgeText = "已拒絕";
            badgeColor = "bg-rose-100 text-rose-700";
        } else if (m.task_id) {
            badgeText = "已派出非同步任務";
            badgeColor = "bg-blue-100 text-blue-700";
        }

        const progressText = m.task_id ? state.taskProgress.get(m.task_id) : null;
        const summary = el("div", {
            class:
                "flex items-center gap-2 text-[11px] " + badgeColor +
                " rounded px-2 py-1 cursor-pointer select-none",
        }, [
            el("i", { class: "fa-solid fa-screwdriver-wrench" }),
            el("span", { class: "font-semibold" }, badgeText),
            m.task_id
                ? el("span", { class: "font-mono opacity-70" }, m.task_id.slice(0, 8))
                : null,
            progressText
                ? el("span", { class: "ml-auto font-mono" }, progressText)
                : null,
        ]);

        // 摺疊區 — 預設收起,點 summary 切換
        const detail = el("pre", {
            class:
                "hidden text-[10px] bg-stone-50 border border-stone-200 " +
                "rounded p-2 mt-1 overflow-x-auto whitespace-pre-wrap " +
                "break-words max-h-48",
        }, m.content || "");
        summary.addEventListener("click", () => {
            detail.classList.toggle("hidden");
        });

        const wrap = el("div", { class: "flex flex-col gap-1 px-2" }, [summary, detail]);

        // 加 approve/reject 按鈕(只在 pending 且 awaiting 時)
        if (isPending && isAwaiting) {
            const actions = el("div", { class: "flex gap-1 mt-1" }, [
                el("button", {
                    type: "button",
                    class:
                        "px-2 py-1 text-[11px] bg-rose-100 hover:bg-rose-200 " +
                        "text-rose-700 rounded",
                    onclick: () => resolvePending(m.pending_action_id, "reject"),
                }, "拒絕"),
                el("button", {
                    type: "button",
                    class:
                        "px-2 py-1 text-[11px] bg-amber-500 hover:bg-amber-600 " +
                        "text-white rounded",
                    onclick: () => openConfirmModal(m),
                }, "同意…"),
            ]);
            wrap.appendChild(actions);
        }

        return wrap;
    }

    function renderSystemMessage(text, color = "stone") {
        const node = el("div", {
            class: `text-center text-xs text-${color}-500 italic py-1`,
        }, text);
        refs.body.appendChild(node);
        refs.body.scrollTop = refs.body.scrollHeight;
    }

    function recomputeCost() {
        state.totalCostUsd = 0;
        for (const m of state.messages) {
            if (m.usage && m.usage.cost_usd) {
                state.totalCostUsd += Number(m.usage.cost_usd);
            }
        }
    }

    function renderCost() {
        if (!refs.costLabel) return;
        refs.costLabel.textContent =
            `本對話成本:$${state.totalCostUsd.toFixed(6)}`;
    }

    function updateUiBusy() {
        if (!refs.sendBtn) return;
        refs.sendBtn.disabled = state.sending;
        refs.textarea.disabled = state.sending;
        refs.fab.disabled = state.sending;
    }

    // ── Boot ───────────────────────────────────────────────────────

    function mount() {
        // 登入前不掛載 — 沿用既有 ``html[data-auth="out"]`` pattern
        // 但保險起見,先建好但 CSS hidden
        buildFab();
        buildPanel();
        buildSessionSidebar();
        buildConfirmModal();

        // 登入前隱藏(沿用既有約定:auth out 時不顯示 FAB)
        const style = document.createElement("style");
        style.textContent =
            'html[data-auth="out"] #agentChatPanel, ' +
            'html[data-auth="out"] button[aria-label="開啟 AI 助手"] { ' +
            '  display: none !important; ' +
            '}';
        document.head.appendChild(style);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mount);
    } else {
        mount();
    }
})();
