# AutoTest — 自動化測試平台

> 本專案所有文件均以繁體中文撰寫；本檔為文件總索引。

## 主要文件

| 文件 | 說明 |
|---|---|
| [README.md](README.md) | 平台介紹、快速開始、v1.1.9 功能一覽、技術架構、安全強化、FAQ |
| [操作說明.md](操作說明.md) | 完整使用者操作手冊（13 個分頁的功能對照 + CI/CD 整合範例） |
| [SECURITY.md](SECURITY.md) | 安全漏洞回報政策、OIDC 強化、自架部署安全建議 |
| [LICENSES.md](LICENSES.md) | 第三方授權與 SaaS 商業使用稽核（60+ 元件） |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 貢獻者行為準則 |

## 營運文件

| 文件 | 說明 |
|---|---|
| [docs/ops/bootstrap.md](docs/ops/bootstrap.md) | 首次部署操作手冊（預設帳號、Zoho SSO、忘記密碼恢復、HTTPS 自簽憑證） |
| [docs/ops/data-safety.md](docs/ops/data-safety.md) | 資料安全、備份機制、Replica 熱備、安全 Rebuild、還原流程 |
| [docs/ops/backup-drill.md](docs/ops/backup-drill.md) | 備份還原演習 SOP（季度執行） |
| [docs/ops/backup-drill-history.md](docs/ops/backup-drill-history.md) | 備份演習紀錄（僅追加） |

## 架構與內部文件

| 文件 | 說明 |
|---|---|
| [frontend/js/README.md](frontend/js/README.md) | 前端模組化架構（RFC-1，Phase 1 核心工具） |
| [backend/app/domains/README.md](backend/app/domains/README.md) | 後端領域驅動設計規劃（RFC-10，延遲實施） |
| [deploy/helm/autotest/README.md](deploy/helm/autotest/README.md) | Kubernetes Helm Chart（骨架） |

## 議題模板

| 文件 | 說明 |
|---|---|
| [.github/ISSUE_TEMPLATE/bug_report.md](.github/ISSUE_TEMPLATE/bug_report.md) | 錯誤回報模板（含環境資訊欄位） |
