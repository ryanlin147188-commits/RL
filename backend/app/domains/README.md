# 後端領域驅動架構（RFC-10）— 延遲實施

本目錄保留給 RFC-10 所規劃的 per-domain 重構。**目前刻意保持空白。**

---

## 延遲原因

RFC-10 計劃將約 37 個 model 檔、20 個 router 檔，以及 services / schemas 目錄整體遷移到 per-domain 資料夾（`domains/projects/`、`domains/testcases/` 等）。這是一個機械性的 8–12 天重構工程，但有以下障礙：

1. **與進行中的 RFC-1 前端拆分衝突**：大量 diff 同時存在會讓 review 難度倍增。
2. **破壞所有現有 import 路徑**：其他所有 RFC 都刻意設計為不強迫同時產生大量變動，而 RFC-10 無法做到這一點。
3. **每工作天的效益低於其他待辦 RFC**：現有的 flat 結構仍可導航；DDD 帶來的是漸進式清晰度，而非缺少的功能。

---

## 何時重新評估

符合以下任一條件時啟動：

- Backend `routers/` 超過約 30 個檔案
- 同一週內有兩位貢獻者在同一個 router 發生 merge conflict
- 新貢獻者花費超過 2 天才能搞清楚「X 在哪裡」
- RFC-1 前端拆分已完整落地（避免同時產生大量 churn）

---

## 預計實施方式

根據 RFC，遷移以 per-domain 方式進行，而非大爆炸式重寫：

1. 一次為一個 domain 建立空的 `domains/<x>/`
2. 將 `models/<x>.py`、`routers/<x>.py`、`services/<x>_service.py`、`schemas/<x>.py` 移入 `domains/<x>/{models,router,service,schemas}.py`
3. 在舊路徑加入重新匯出的 shim，讓不相關的 import 繼續運作（`# from app.models.x import X  # legacy; use app.domains.x`）
4. 執行完整測試套件 + ruff 邊界 lint——兩者都必須保持通過
5. 對下一個 domain 重複以上步驟

`scripts/audit_endpoints.py`（RFC-5 工作成果）中的 AST walker 可重用來自動產生 legacy 重新匯出模組。

---

## 負責人

未指派。請在 issue tracker 建立追蹤任務，標題為：`RFC-10 延遲實施 — 詳見 backend/app/domains/README.md`。
