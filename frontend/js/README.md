# 前端模組化架構（RFC-1）

> 適用版本：v1.1.9+ · `index.html` 目前約 21,000+ 行

本目錄是 AutoTest SPA 模組化的新家，從原本超過 21,000 行的單一 `index.html` 逐步拆分而來。

第一階段（目前狀態）只提供每個 view 都需要的**核心工具**；各 view 本身（含 v1.1.9 新增的測試看版、缺陷管理、測試時程、Mock、畫面比對）暫時仍以 inline script 形式保留在 `index.html` 中。

---

## 為什麼要模組化

現有 `index.html` 存在以下問題：

- **500+ 個全域函式**（v1.1.9 後新增 testkanban / defects / testschedule / mock / pictureDiff 各 view 約 100+ 個）：IDE 搜尋與 diff 的負擔隨著規模線性成長
- **20+ 個重複的 CRUD modal 區塊**：每次修改都要改 10+ 地方（待辦 / 缺陷 / 時程 modal 結構幾乎相同）
- **全域變數**（`window.currentProjectId`、`_caches`）：競態條件風險
- **無型別提示、無錯誤邊界、無 code splitting**

分階段模組化讓每個 PR 都保持可 review 的大小。

---

## 目錄結構

```
js/
├── core/
│   ├── api.js          # apiFetch + 401 refresh + ApiError
│   ├── auth.js         # token storage + isLoggedIn / isExpiringSoon
│   └── store.js        # pub/sub state，用於 view 間通訊
└── README.md           # 本文件
```

第二階段將加入 `components/`（Modal、Form、Table、Toast）。第三階段將各 view（`projects.js`、`testcases.js` 等）從 `index.html` 移出。

---

## 向後相容性

每個模組在匯出函式的同時，也會將其掛載到 `window.AutoTest.*`，讓 `index.html` 中的 inline script 能逐步採用，不需要大幅改動現有程式碼：

```js
// inline script — 改前
async function loadCases(pid) { /* ad-hoc fetch */ }

// inline script — 改後（採用模組）
async function loadCases(pid) {
  return AutoTest.api.apiFetch(`/api/projects/${pid}/testcases`);
}
```

不需要 `<script type="module">` 改寫即可逐步遷移。

---

## 模組掛載方式

`index.html` 在 `<body>` 頂部載入第一階段核心模組：

```html
<script type="module">
  // 副作用：定義 window.AutoTest.{auth, api, store}
  import "/js/core/auth.js";
  import "/js/core/api.js";
  import "/js/core/store.js";
</script>
```

載入後，頁面任何地方都可以呼叫 `AutoTest.api.apiFetch(...)` 或 `AutoTest.store.set("currentProjectId", id)`。

---

## 各階段規劃

| 階段 | 範圍 | 目標工時 | 目前進度 |
|---|---|---|---|
| **1** | `core/` 工具 + `window.AutoTest` shim | 2 天 | ✅ 已完成（`core/api.js`、`core/auth.js`、`core/store.js`）|
| 2 | `components/`（Modal / Form / Table / Toast）+ login view 作為試點 | 3 天 | ⏳ 尚未啟動 |
| 3 | 將 22 個 view 從 `index.html` 移出（含 v1.1.9 新增的 testkanban / defects / testschedule / mock / pictureDiff），每天 2–3 個 | 6–8 天 | ⏳ 尚未啟動 |
| 4 | 將全域變數替換為 `store.subscribe` | 2–3 天 | ⏳ 尚未啟動 |
| 5 | 選用：加入 ESLint + Prettier（無需建置步驟） | 1–2 天 | ⏳ 尚未啟動 |

每個階段結束時 `index.html` 仍可正常運作——不做大爆炸式重寫。

**啟動下一階段的條件**：當主分支累積了 3 個以上需要修改同一個 inline view 的 PR，且 PR 之間互相衝突嚴重時，就是該推進階段 2 的訊號。

---

## 部署前必跑語法檢查

由於 `index.html` 內含 21,000+ 行 inline JS，純文字編輯容易留下未閉合的括號 / template literal。每次改完 `index.html` **必須**在部署前跑：

```bash
node --check frontend/index.html
```

若有語法錯誤會印出行號，CI 也會在 PR 階段擋下。
