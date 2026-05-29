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

    // v1.2.x:移除聊天框 model picker — model 由「設定 → AI Token」內各
    // provider 的 default_model 決定;沒設過時 backend 走 smart default 撈
    // org enabled provider 的 default_model。聊天框 header 只顯示目前 session
    // 用的 model 名(唯讀)。

    const state = {
        open: false,
        sessionId: null,
        sessionTitle: null,
        sessionModel: null,
        sessionOrgId: null,    // session.organization_id;Skill 列表 fetch 用
        activeSkillId: null,   // 目前 session active skill id
        skills: null,          // org skills cache(載一次就放著);null = 尚未載
        mcpServers: null,      // org MCP servers cache(只給 health icon hover 顯示用)
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
        modelLabel: null,  // 唯讀,顯示目前 session 用什麼 model
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
        // 顯示目前 session 用的 model 名(唯讀)。要換 model 請去「設定 → AI Token」
        // 改各 provider 的 default_model。
        const modelLabel = el("span", {
            class:
                "text-[10px] text-stone-400 font-mono truncate max-w-[160px]",
            title: "目前 session 使用的模型(由設定 → AI Token 的 default_model 決定)",
        }, "");
        // Skill chip:顯示 active skill name + 紫色 dot,點開下拉切換
        const skillChip = el("button", {
            type: "button",
            class:
                "flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-full " +
                "border border-stone-300 bg-white hover:bg-stone-50 text-stone-600 " +
                "max-w-[120px] truncate",
            title: "切換 Skill",
            onclick: (ev) => { ev.stopPropagation(); openSkillPicker(); },
        }, [
            el("span", {
                class: "inline-block w-1.5 h-1.5 rounded-full bg-stone-300",
            }),
            el("span", { class: "truncate" }, "無 Skill"),
        ]);
        refs.skillChip = skillChip;
        refs.skillChipDot = skillChip.children[0];
        refs.skillChipLabel = skillChip.children[1];
        // MCP server 健康指示器(齒輪旁邊小圖示)。hover 顯示連線總數,點擊跳到設定頁
        const mcpHealthBtn = el("button", {
            type: "button",
            class: "text-stone-400 hover:text-emerald-500 p-1 relative",
            title: "MCP servers — 點擊管理",
            onclick: () => {
                // 開新分頁跳到設定 → MCP Servers
                if (typeof window.switchView === "function") {
                    try {
                        window.switchView("userSettings");
                        if (typeof window.settingsSwitchTab === "function") {
                            window.settingsSwitchTab("mcp");
                        }
                    } catch (_) { /* 沒接到 view router 就靜默 */ }
                }
            },
        }, [
            el("i", { class: "fa-solid fa-plug" }),
            el("span", {
                class: "absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-stone-300",
            }),
        ]);
        refs.mcpHealthBtn = mcpHealthBtn;
        refs.mcpHealthDot = mcpHealthBtn.children[1];
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
            skillChip,
            modelLabel,
            mcpHealthBtn,
            historyBtn,
            newBtn,
            closeBtn,
        ]);
        refs.sessionLabel = sessionLabel;
        refs.modelLabel = modelLabel;

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
        // Phase 2b: trigger_keywords 命中 → 在輸入框上方顯示「建議切換 skill」chip
        // debounce 250ms 避免每按一個字都 grep skills(目前最多 9 個 skill,所以 cost
        // 不高,但留 debounce 比較通用)。
        let _suggestTimer = null;
        textarea.addEventListener("input", () => {
            if (_suggestTimer) clearTimeout(_suggestTimer);
            _suggestTimer = setTimeout(() => _updateSuggestionArea(textarea.value), 250);
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
        // 建議切換 skill 的 chip 區(預設 hidden;只有命中 trigger_keywords 時才顯示)
        const suggestionArea = el("div", {
            class: "flex flex-wrap gap-1 mb-1.5 text-[11px] hidden",
        });
        const footer = el("div", {
            class: "border-t border-stone-200 bg-white p-2",
        }, [
            suggestionArea,
            el("div", { class: "flex gap-2 items-stretch" }, [textarea, sendBtn]),
            costLabel,
        ]);
        refs.textarea = textarea;
        refs.sendBtn = sendBtn;
        refs.costLabel = costLabel;
        refs.suggestionArea = suggestionArea;

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

    // sidebar 搜尋過濾 — 即時 filter,記住 sessions cache 避免重打 API
    let _sidebarSessionsCache = [];
    let _sidebarFilter = "";

    function _renderSidebarRows() {
        refs.sidebarBody.replaceChildren();
        const filter = _sidebarFilter.trim().toLowerCase();
        const filtered = !filter
            ? _sidebarSessionsCache
            : _sidebarSessionsCache.filter(s => (s.title || "").toLowerCase().includes(filter));
        if (filtered.length === 0) {
            refs.sidebarBody.appendChild(
                el("div", { class: "text-xs text-stone-400 italic p-3" },
                    filter ? "沒有符合的對話" : "尚無歷史對話")
            );
            return;
        }
        // 依日期分群:今天 / 昨天 / 本週 / 更早
        const now = Date.now();
        const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);
        const yesterdayStart = todayStart.getTime() - 86400_000;
        const weekStart = todayStart.getTime() - 6 * 86400_000;
        const groups = { today: [], yesterday: [], week: [], older: [] };
        for (const s of filtered) {
            const t = s.updated_at ? new Date(s.updated_at).getTime() : 0;
            if (t >= todayStart.getTime()) groups.today.push(s);
            else if (t >= yesterdayStart) groups.yesterday.push(s);
            else if (t >= weekStart) groups.week.push(s);
            else groups.older.push(s);
        }
        const groupLabels = { today: "今天", yesterday: "昨天", week: "本週", older: "更早" };
        for (const key of ["today", "yesterday", "week", "older"]) {
            if (!groups[key].length) continue;
            refs.sidebarBody.appendChild(
                el("div", { class: "text-[10px] uppercase font-semibold text-stone-400 px-3 pt-3 pb-1" },
                    groupLabels[key])
            );
            for (const s of groups[key]) {
                refs.sidebarBody.appendChild(_buildSessionRow(s));
            }
        }
    }

    function _buildSessionRow(s) {
        const isCurrent = s.id === state.sessionId;
        const titleEl = el("div", {
            class: "text-xs font-semibold text-stone-700 truncate",
        }, s.title || "未命名對話");
        const metaEl = el("div", {
            class: "text-[10px] text-stone-400",
        }, [
            s.model || "預設模型",
            " · ",
            formatRelativeTime(s.updated_at),
        ].join(""));
        const mainCol = el("div", { class: "flex-1 min-w-0" }, [titleEl, metaEl]);
        const delBtn = el("button", {
            type: "button",
            class: "shrink-0 p-1 text-stone-300 hover:text-rose-500 hover:bg-rose-50 rounded transition",
            title: "刪除此對話",
            "aria-label": `刪除對話 ${s.title || "未命名"}`,
            // 修 a11y:鍵盤 focus 時也顯示(原本只 hover 顯示,鍵盤族看不到)
            style: "opacity:0; transition:opacity .15s",
        }, [el("i", { class: "fa-solid fa-trash text-xs", "aria-hidden": "true" })]);
        delBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            deleteSessionFromSidebar(s.id, s.title || "未命名對話");
        });
        delBtn.addEventListener("focus", () => { delBtn.style.opacity = "1"; });
        delBtn.addEventListener("blur", () => { delBtn.style.opacity = "0"; });
        const row = el("div", {
            class:
                "flex items-start gap-2 px-3 py-2 border-b border-stone-100 " +
                "cursor-pointer hover:bg-amber-50 focus:outline-amber-300 " +
                (isCurrent ? "bg-amber-50" : ""),
            tabindex: "0",
            role: "button",
            "aria-current": isCurrent ? "true" : "false",
            onclick: () => switchToSession(s.id),
        }, [mainCol, delBtn]);
        row.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                switchToSession(s.id);
            } else if (e.key === "Delete" || e.key === "Backspace") {
                e.preventDefault();
                deleteSessionFromSidebar(s.id, s.title || "未命名對話");
            }
        });
        row.addEventListener("mouseenter", () => { delBtn.style.opacity = "1"; });
        row.addEventListener("mouseleave", () => { if (document.activeElement !== delBtn) delBtn.style.opacity = "0"; });
        return row;
    }

    async function refreshSessionsList() {
        try {
            const sessions = await api("/api/agent/sessions?limit=50");
            _sidebarSessionsCache = Array.isArray(sessions) ? sessions : [];
            // 加搜尋框(只加一次)
            if (!refs.sidebarBody.parentElement.querySelector("[data-sidebar-search]")) {
                const search = document.createElement("input");
                search.type = "search";
                search.placeholder = "搜尋對話標題…";
                search.setAttribute("data-sidebar-search", "1");
                search.setAttribute("aria-label", "搜尋對話");
                search.className = "w-[calc(100%-1.5rem)] mx-3 mt-2 mb-1 px-2 py-1 text-xs border border-stone-200 rounded focus:outline-amber-400";
                search.addEventListener("input", () => {
                    _sidebarFilter = search.value || "";
                    _renderSidebarRows();
                });
                refs.sidebarBody.parentElement.insertBefore(search, refs.sidebarBody);
            }
            _renderSidebarRows();
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

    async function deleteSessionFromSidebar(sessionId, title) {
        if (state.sending) return;
        const confirmFn = (typeof window !== 'undefined' && window.confirmDialog) || null;
        if (confirmFn) {
            const ok = await confirmFn({
                title: '刪除對話',
                body: `確定刪除對話「${title}」?\n所有訊息會被一併刪除(不可復原)。`,
                confirmLabel: '刪除',
                danger: true,
            });
            if (!ok) return;
        } else if (!confirm(`確定刪除對話「${title}」?\n所有訊息會被一併刪除(不可復原)。`)) {
            return;
        }
        try {
            await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}`, {
                method: "DELETE",
            });
            // 刪到的是當前 session → 清掉狀態,自動跳到別的或建新
            if (sessionId === state.sessionId) {
                state.sessionId = null;
                state.sessionTitle = null;
                state.sessionModel = null;
                state.sessionOrgId = null;
                state.activeSkillId = null;
                state.messages = [];
                state.totalCostUsd = 0;
                state.pendingActions.clear();
                for (const ws of state.taskSockets.values()) {
                    try { ws.close(); } catch (_) { /* */ }
                }
                state.taskSockets.clear();
                state.taskProgress.clear();
                _updateModelLabel();
                _updateSkillChip();
                state.mcpServers = null;
                _updateMcpHealth();
                refs.body.replaceChildren();
                renderCost();
                // 重新從 sessions list 抓最近一個或自動建新
                await ensureSessionThenRefresh();
            }
            // 重整 sidebar 列表
            await refreshSessionsList();
        } catch (e) {
            renderSystemMessage(`刪除失敗:${e.message || e}`, "error");
        }
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
            state.sessionOrgId = session.organization_id || null;
            state.activeSkillId = session.active_skill_id || null;
            _updateModelLabel();
            await loadSkillsForSession();
            await loadMcpServersForSession();
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

    function _updateModelLabel() {
        if (!refs.modelLabel) return;
        refs.modelLabel.textContent = state.sessionModel ? "· " + state.sessionModel : "";
    }

    function _updateSkillChip() {
        if (!refs.skillChip) return;
        const active = (state.skills || []).find((s) => s.id === state.activeSkillId);
        if (active) {
            refs.skillChipLabel.textContent = active.name;
            refs.skillChipDot.classList.remove("bg-stone-300");
            refs.skillChipDot.classList.add("bg-violet-500");
            refs.skillChip.classList.add("border-violet-300");
            refs.skillChip.classList.remove("border-stone-300");
            // active state — 給 panel 加紫色外框暗示
            if (refs.panel) {
                refs.panel.classList.add("ring-2", "ring-violet-200");
            }
        } else {
            refs.skillChipLabel.textContent = "無 Skill";
            refs.skillChipDot.classList.add("bg-stone-300");
            refs.skillChipDot.classList.remove("bg-violet-500");
            refs.skillChip.classList.remove("border-violet-300");
            refs.skillChip.classList.add("border-stone-300");
            if (refs.panel) {
                refs.panel.classList.remove("ring-2", "ring-violet-200");
            }
        }
    }

    // Phase 2b: 把 textarea 內容跟所有 skill 的 trigger_keywords 比對,命中就在
    // textarea 上方顯示 chip 建議切換。不會自動套用 — 永遠等使用者點 chip 才切。
    // 命中規則:文字含關鍵字(case-insensitive,substring match)。已 active 的
    // skill 不會出現在建議內(切過去無意義)。最多顯示 3 個建議。
    function _findSuggestedSkills(text) {
        if (!text || typeof text !== "string") return [];
        const t = text.toLowerCase();
        if (t.trim().length < 2) return [];
        const skills = state.skills || [];
        const out = [];
        for (const s of skills) {
            if (s.id === state.activeSkillId) continue;
            const kws = s.trigger_keywords || [];
            if (!Array.isArray(kws) || kws.length === 0) continue;
            for (const kw of kws) {
                if (!kw) continue;
                if (t.includes(String(kw).toLowerCase())) {
                    out.push({ id: s.id, name: s.name, matched: kw });
                    break;
                }
            }
            if (out.length >= 3) break;
        }
        return out;
    }

    function _updateSuggestionArea(text) {
        if (!refs.suggestionArea) return;
        const matches = _findSuggestedSkills(text);
        refs.suggestionArea.innerHTML = "";
        if (matches.length === 0) {
            refs.suggestionArea.classList.add("hidden");
            return;
        }
        refs.suggestionArea.classList.remove("hidden");
        // 前綴提示文字
        refs.suggestionArea.appendChild(
            el("span", { class: "text-stone-500 self-center mr-1" }, "💡 建議切換:")
        );
        for (const m of matches) {
            const chip = el("button", {
                type: "button",
                class:
                    "px-2 py-0.5 rounded-full border border-violet-300 bg-violet-50 " +
                    "text-violet-700 hover:bg-violet-100 transition-colors",
                title: `匹配關鍵字「${m.matched}」 — 點擊切換到 ${m.name} skill`,
                onclick: () => {
                    setActiveSkill(m.id);
                    // 切換後立刻清掉建議(已 active 不會再 match 自己)
                    refs.suggestionArea.innerHTML = "";
                    refs.suggestionArea.classList.add("hidden");
                },
            }, m.name);
            refs.suggestionArea.appendChild(chip);
        }
    }

    async function loadSkillsForSession(force = false) {
        if (!state.sessionOrgId) {
            state.skills = [];
            _updateSkillChip();
            return;
        }
        if (state.skills !== null && !force) return;
        try {
            const list = await api(
                `/api/v1/orgs/${encodeURIComponent(state.sessionOrgId)}/skills`
            );
            // 只列 enabled 的給 picker — disabled 還是給 settings 頁管理
            state.skills = (list || []).filter((s) => s.enabled);
        } catch (e) {
            // 無權限或 endpoint 還沒部署時靜默退化(不擋整個聊天框)
            state.skills = [];
        }
        _updateSkillChip();
    }

    function openSkillPicker() {
        if (state.sending) return;
        // 已開 → 收起
        if (refs.skillPicker && refs.skillPicker.parentNode) {
            refs.skillPicker.remove();
            refs.skillPicker = null;
            return;
        }
        const picker = el("div", {
            class:
                "absolute z-20 bg-white border border-stone-200 rounded-lg shadow-lg " +
                "py-1 text-xs max-h-[280px] overflow-y-auto",
            style: "min-width:180px; max-width:240px;",
        });
        // 「無 Skill」清空選項
        const clearItem = el("button", {
            type: "button",
            class:
                "w-full text-left px-3 py-1.5 hover:bg-stone-100 flex items-center " +
                "gap-2 text-stone-600",
            onclick: () => setActiveSkill(null),
        }, [
            el("span", { class: "inline-block w-1.5 h-1.5 rounded-full bg-stone-300" }),
            el("span", {}, "無 Skill"),
        ]);
        picker.appendChild(clearItem);
        const skills = state.skills || [];
        if (skills.length === 0) {
            picker.appendChild(el("div", {
                class: "px-3 py-2 text-stone-400",
            }, "尚未建立 Skill"));
        } else {
            picker.appendChild(el("div", {
                class: "border-t border-stone-100 my-1",
            }));
        }
        for (const s of skills) {
            const isActive = s.id === state.activeSkillId;
            const item = el("button", {
                type: "button",
                class:
                    "w-full text-left px-3 py-1.5 hover:bg-violet-50 flex items-center " +
                    "gap-2 " + (isActive ? "bg-violet-50 text-violet-700" : "text-stone-700"),
                title: s.description || s.name,
                onclick: () => setActiveSkill(s.id),
            }, [
                el("span", {
                    class:
                        "inline-block w-1.5 h-1.5 rounded-full " +
                        (isActive ? "bg-violet-500" : "bg-stone-300"),
                }),
                el("span", { class: "truncate" }, s.name),
            ]);
            picker.appendChild(item);
        }
        // 放在 chip 正下方;ParentNode 用 panel 當錨點(panel 本來就 position:fixed)
        const rect = refs.skillChip.getBoundingClientRect();
        picker.style.position = "fixed";
        picker.style.top = (rect.bottom + 4) + "px";
        picker.style.left = rect.left + "px";
        document.body.appendChild(picker);
        refs.skillPicker = picker;
        // 點外面關掉
        const off = (ev) => {
            if (!picker.contains(ev.target) && ev.target !== refs.skillChip) {
                picker.remove();
                refs.skillPicker = null;
                document.removeEventListener("mousedown", off, true);
            }
        };
        setTimeout(() => document.addEventListener("mousedown", off, true), 0);
    }

    function _updateMcpHealth() {
        if (!refs.mcpHealthDot || !refs.mcpHealthBtn) return;
        const servers = state.mcpServers || [];
        if (servers.length === 0) {
            // 沒設 MCP server → 維持灰色,title 改成提示
            refs.mcpHealthDot.classList.remove("bg-emerald-500", "bg-rose-500", "bg-amber-500");
            refs.mcpHealthDot.classList.add("bg-stone-300");
            refs.mcpHealthBtn.title = "尚未設定 MCP server — 點擊管理";
            return;
        }
        const enabled = servers.filter((s) => s.enabled);
        const connected = enabled.filter((s) => s.last_health === "connected").length;
        const errored = enabled.filter((s) => s.last_health === "error").length;
        refs.mcpHealthDot.classList.remove("bg-stone-300", "bg-emerald-500", "bg-rose-500", "bg-amber-500");
        if (errored > 0) {
            refs.mcpHealthDot.classList.add("bg-rose-500");
        } else if (connected === enabled.length && enabled.length > 0) {
            refs.mcpHealthDot.classList.add("bg-emerald-500");
        } else {
            refs.mcpHealthDot.classList.add("bg-amber-500");
        }
        refs.mcpHealthBtn.title = `MCP — ${connected}/${enabled.length} connected${errored > 0 ? `, ${errored} error` : ''}`;
    }

    async function loadMcpServersForSession(force = false) {
        if (!state.sessionOrgId) {
            state.mcpServers = [];
            _updateMcpHealth();
            return;
        }
        if (state.mcpServers !== null && !force) return;
        try {
            const list = await api(
                `/api/v1/orgs/${encodeURIComponent(state.sessionOrgId)}/mcp-servers`
            );
            state.mcpServers = list || [];
        } catch (e) {
            state.mcpServers = [];
        }
        _updateMcpHealth();
    }

    async function setActiveSkill(skillId) {
        if (!state.sessionId) return;
        if (refs.skillPicker) { refs.skillPicker.remove(); refs.skillPicker = null; }
        try {
            await api(
                `/api/v1/agent/sessions/${encodeURIComponent(state.sessionId)}/skill`,
                {
                    method: "POST",
                    body: JSON.stringify({ skill_id: skillId }),
                }
            );
            state.activeSkillId = skillId || null;
            _updateSkillChip();
        } catch (e) {
            renderSystemMessage(`切換 Skill 失敗:${e.message}`, "rose");
        }
    }

    async function startNewSession() {
        try {
            state.sending = true;
            updateUiBusy();
            // 不指定 model — 讓 backend 走 smart default 撈該 org enabled provider
            // 的 default_model(設定 → AI Token 內各 provider 的「預設模型」)
            const session = await api("/api/agent/sessions", {
                method: "POST",
                body: JSON.stringify({}),
            });
            state.sessionId = session.id;
            state.sessionTitle = session.title || "新對話";
            state.sessionModel = session.model;
            state.sessionOrgId = session.organization_id || null;
            state.activeSkillId = session.active_skill_id || null;
            _updateModelLabel();
            await loadSkillsForSession();
            await loadMcpServersForSession();
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
                state.sessionOrgId = sessions[0].organization_id || null;
                state.activeSkillId = sessions[0].active_skill_id || null;
                _updateModelLabel();
                await loadSkillsForSession();
            await loadMcpServersForSession();
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
            // 平行抓 session metadata(autofallback 可能改了 model)+ messages
            const [session, msgs] = await Promise.all([
                api(`/api/agent/sessions/${encodeURIComponent(state.sessionId)}`)
                    .catch(() => null),
                api(`/api/agent/sessions/${encodeURIComponent(state.sessionId)}/messages?limit=200`),
            ]);
            if (session) {
                state.sessionModel = session.model;
                if (session.title) state.sessionTitle = session.title;
                state.sessionOrgId = session.organization_id || null;
                state.activeSkillId = session.active_skill_id || null;
                _updateModelLabel();
                // org 改變(切 session)或還沒載過 skills 才 fetch;否則只更新 chip
                if (state.skills === null) {
                    await loadSkillsForSession();
                } else {
                    _updateSkillChip();
                }
                if (state.mcpServers === null) {
                    await loadMcpServersForSession();
                } else {
                    _updateMcpHealth();
                }
            }
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
            // 對「未設定 LLM API key」這類 400,給明確指引而非泛用「送出失敗」
            const msg = String(e.message || "");
            const noKey =
                /無可用的 LLM provider|未設定 API key|未設定.*API|無可用.*provider/i.test(msg);
            if (noKey) {
                renderSystemMessage(
                    "⚠ 尚未設定 LLM API key,因此無法對話。\n" +
                    "請聯絡管理員設定 Anthropic / OpenAI / Google 任一家的 key。\n" +
                    "(superuser 可呼叫 PUT /api/admin/llm-providers/global/{provider},\n" +
                    " 或 org admin 呼叫 PUT /api/settings/llm-providers/{provider})",
                    "warn"
                );
            } else {
                renderSystemMessage(`送出失敗:${msg}`, "error");
            }
        } finally {
            state.sending = false;
            updateUiBusy();
        }
    }

    // ── 二次確認 modal ──────────────────────────────────────────

    // destructive tool 視覺強調 — 命中以下 prefix 一律標紅 + 預設 focus 在「拒絕」,
    // 避免使用者習慣性按 Enter 誤觸 destructive action。
    const DESTRUCTIVE_TOOL_PREFIXES = [
        "delete_", "remove_", "drop_", "purge_", "wipe_", "reset_",
        "move_", "submit_review",  // submit_review 會鎖死 entity → 等同 destructive
    ];
    function _isDestructiveTool(name) {
        const n = String(name || "").toLowerCase();
        return DESTRUCTIVE_TOOL_PREFIXES.some(p => n.startsWith(p));
    }
    function _formatArgValue(v) {
        // arg value 用 textContent 渲染(已是純文字),但對 nested object 用 JSON 美化
        if (v === null || v === undefined) return "(空)";
        if (typeof v === "string") return v;
        if (typeof v === "number" || typeof v === "boolean") return String(v);
        try { return JSON.stringify(v, null, 0); } catch (_) { return String(v); }
    }

    let _confirmCountdownTimer = null;
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

        // backend 已把 __integrity__ HMAC 從 response 剝掉;這裡再防禦性過濾一次
        if (info.arguments && typeof info.arguments === "object" && "__integrity__" in info.arguments) {
            const { __integrity__, ...rest } = info.arguments;
            info.arguments = rest;
        }
        const destructive = _isDestructiveTool(info.tool_name);

        // arguments key-value table(取代 raw JSON dump)
        const argsList = el("ul", { class: "text-xs text-stone-700 mt-2 space-y-1" });
        const entries = Object.entries(info.arguments).slice(0, 12);
        if (entries.length === 0) {
            argsList.appendChild(el("li", { class: "text-stone-400 italic" }, "(無參數)"));
        } else {
            for (const [k, v] of entries) {
                const row = el("li", { class: "flex gap-2" }, [
                    el("span", { class: "font-semibold text-stone-700 shrink-0" }, k + ":"),
                ]);
                const vSpan = el("span", { class: "text-stone-600 break-all" });
                vSpan.textContent = _formatArgValue(v);
                row.appendChild(vSpan);
                argsList.appendChild(row);
            }
        }

        // 倒數計時 — 顯示剩餘秒數,到期自動 reject(後端標 expired,UI 顯示 "已過期")
        const countdownEl = el("p", {
            class: destructive ? "text-[11px] text-rose-600 mt-2 font-semibold" : "text-[11px] text-amber-600 mt-2",
        }, "");
        function _tickCountdown() {
            if (!info.expires_at) { countdownEl.textContent = ""; return; }
            const remain = Math.max(0, Math.floor((new Date(info.expires_at).getTime() - Date.now()) / 1000));
            if (remain <= 0) {
                countdownEl.textContent = "⏱ 已過期;請重新請求";
                if (_confirmCountdownTimer) { clearInterval(_confirmCountdownTimer); _confirmCountdownTimer = null; }
                // 自動關閉 modal
                setTimeout(() => closeConfirmModal(), 1500);
                return;
            }
            const mm = String(Math.floor(remain / 60)).padStart(1, "0");
            const ss = String(remain % 60).padStart(2, "0");
            countdownEl.textContent = `⏱ 剩餘 ${mm}:${ss} 後自動取消`;
        }
        _tickCountdown();
        if (_confirmCountdownTimer) clearInterval(_confirmCountdownTimer);
        if (info.expires_at) _confirmCountdownTimer = setInterval(_tickCountdown, 1000);

        const onApprove = async () => {
            if (_confirmCountdownTimer) { clearInterval(_confirmCountdownTimer); _confirmCountdownTimer = null; }
            await resolvePending(actionId, "approve");
            closeConfirmModal();
        };
        const onReject = async () => {
            if (_confirmCountdownTimer) { clearInterval(_confirmCountdownTimer); _confirmCountdownTimer = null; }
            await resolvePending(actionId, "reject");
            closeConfirmModal();
        };

        const rejectBtn = el("button", {
            type: "button",
            class:
                "flex-1 py-2 bg-stone-100 hover:bg-stone-200 text-stone-700 " +
                "rounded text-sm font-semibold border border-stone-300",
            onclick: onReject,
        }, "拒絕");
        const approveBtn = el("button", {
            type: "button",
            class: destructive
                ? "flex-1 py-2 bg-rose-600 hover:bg-rose-700 text-white rounded text-sm font-semibold shadow-sm"
                : "flex-1 py-2 bg-amber-500 hover:bg-amber-600 text-white rounded text-sm font-semibold",
            onclick: onApprove,
        }, destructive ? "仍要執行" : "同意執行");

        refs.confirmBackdrop.style.display = "flex";
        refs.confirmDialog.setAttribute("role", "alertdialog");
        refs.confirmDialog.setAttribute("aria-modal", "true");
        refs.confirmDialog.replaceChildren(
            el("div", { class: "flex items-center gap-2 mb-2" }, [
                el("i", {
                    class: destructive
                        ? "fa-solid fa-triangle-exclamation text-rose-600 text-lg"
                        : "fa-solid fa-shield-halved text-amber-500 text-lg",
                    "aria-hidden": "true",
                }),
                el("h3", {
                    class: destructive
                        ? "text-base font-bold text-rose-700"
                        : "text-base font-bold text-stone-800",
                }, destructive ? "即將執行破壞性操作" : "需要確認"),
            ]),
            el("p", { class: "text-sm text-stone-600 leading-relaxed" }, "AI 想要執行"),
            el("p", { class: "text-sm font-mono bg-stone-100 px-2 py-1 rounded my-2" }, info.tool_name),
            argsList,
            countdownEl,
            el("div", { class: "flex gap-2 mt-4" }, [rejectBtn, approveBtn]),
        );
        // 預設 focus:destructive 落在「拒絕」,一般在「同意執行」
        requestAnimationFrame(() => (destructive ? rejectBtn : approveBtn).focus());
        // 已在函式頂端設 display:flex,這裡 no-op(保留結構讓 git diff 清楚)
    }

    function closeConfirmModal() {
        if (_confirmCountdownTimer) { clearInterval(_confirmCountdownTimer); _confirmCountdownTimer = null; }
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

        // 偵測 MCP tool result(後端會用 <external_tool_data server="X" tool="Y"> 包)
        // 包括 pending placeholder 也偵測 — 看 parsed.tool_name 是否 mcp__ 開頭
        let mcpServerName = null;
        if (m.content && typeof m.content === "string") {
            const match = m.content.match(/^<external_tool_data\s+server="([^"]+)"/);
            if (match) mcpServerName = match[1];
        }
        if (!mcpServerName && parsed && typeof parsed.tool_name === "string") {
            const m2 = parsed.tool_name.match(/^mcp__([^_]+(?:_[^_]+)*)__/);
            if (m2) mcpServerName = m2[1];
        }

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
        } else if (mcpServerName) {
            badgeText = "MCP";
            badgeColor = "bg-emerald-100 text-emerald-700";
        }

        const progressText = m.task_id ? state.taskProgress.get(m.task_id) : null;
        const summary = el("div", {
            class:
                "flex items-center gap-2 text-[11px] " + badgeColor +
                " rounded px-2 py-1 cursor-pointer select-none",
        }, [
            el("i", {
                class: mcpServerName ? "fa-solid fa-plug" : "fa-solid fa-screwdriver-wrench",
            }),
            el("span", { class: "font-semibold" }, badgeText),
            mcpServerName
                ? el("span", { class: "font-mono opacity-80" }, mcpServerName)
                : null,
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

        const wrapClass = mcpServerName
            ? "flex flex-col gap-1 pl-3 pr-2 border-l-2 border-emerald-300"
            : "flex flex-col gap-1 px-2";
        const wrap = el("div", { class: wrapClass }, [summary, detail]);

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

    // level: "info"(灰) / "warn"(琥珀) / "error"(紅) — 改 inline 避免動態 tailwind class 缺漏
    const SYSTEM_MSG_COLORS = {
        info:  { fg: "#78716c", bg: "transparent",    border: "transparent" },  // stone-500
        warn:  { fg: "#b45309", bg: "#fffbeb",        border: "#fde68a" },      // amber
        error: { fg: "#be123c", bg: "#fff1f2",        border: "#fecdd3" },      // rose
    };
    function renderSystemMessage(text, level = "info") {
        // 兼容舊呼叫:傳 "rose" / "amber" / "stone" → 映射
        const legacy = { rose: "error", amber: "warn", stone: "info" };
        if (legacy[level]) level = legacy[level];
        const c = SYSTEM_MSG_COLORS[level] || SYSTEM_MSG_COLORS.info;
        const node = el("div", {
            class: "text-center text-xs italic",
            style:
                `color:${c.fg}; background:${c.bg}; border:1px solid ${c.border};` +
                " border-radius:6px; padding:6px 10px; margin:4px 0;" +
                " white-space:pre-wrap;",
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
