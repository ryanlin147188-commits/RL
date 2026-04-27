# 第三方授權與商業使用稽核 — AutoTest v1.0

> **部署情境：對外提供 SaaS / 雲服務**
> 本文件為 AutoTest v1.0 所有第三方相依元件的授權整理，協助評估商業使用合規性。
> ⚠️ **這份文件是技術層面的整理，不是法律意見。實際商業部署前，建議由貴公司法務或熟悉開源授權的顧問進行最終 review。**

> 🟢 **本版本（PostgreSQL / Valkey / SeaweedFS stack）已將具有 Copyleft 風險的元件全部替換**：
> - MySQL（GPLv2）→ **PostgreSQL**（PostgreSQL License，類 MIT）
> - Redis 7.4+（SSPLv1）→ **Valkey**（BSD-3-Clause，由 Linux Foundation 領導的 Redis fork）
> - MinIO（AGPLv3）→ **SeaweedFS**（Apache 2.0）

---

## ✅ 結論摘要

**所有 50+ 元件都是開源、可在 SaaS 情境商業使用**，無 AGPL / SSPL / GPL 風險。
**只剩 1 個元件需要做 attribution**：

| 元件 | 重點 |
|---|---|
| **Font Awesome 6 Free** | 圖示 CC BY 4.0 — Footer 須加 attribution（一行字即可） |

**Docker Desktop vs Docker Engine** 仍是 SaaS 部署的注意事項：生產環境用 Docker Engine（Apache 2.0、免費），開發機才有 Docker Desktop 訂閱問題。

---

## 📋 完整相依清單

### 2.1 Backend Python 相依
來源：[`backend/requirements.txt`](backend/requirements.txt)

| 套件 | 版本 | 授權 | SaaS 商用 |
|---|---|---|---|
| fastapi | 0.115.0 | MIT | ✅ |
| uvicorn[standard] | 0.30.6 | BSD-3-Clause | ✅ |
| sqlalchemy[asyncio] | 2.0.35 | MIT | ✅ |
| asyncpg | 0.30.0 | Apache 2.0 | ✅ |
| psycopg[binary] | 3.2.3 | LGPL-3.0+（**僅 client lib，不感染應用**） | ✅ |
| pydantic | 2.9.2 | MIT | ✅ |
| pydantic-settings | 2.5.2 | MIT | ✅ |
| python-dotenv | 1.0.1 | BSD-3-Clause | ✅ |
| python-multipart | 0.0.12 | Apache 2.0 | ✅ |
| aiofiles | 24.1.0 | Apache 2.0 | ✅ |
| boto3 | 1.35.36 | Apache 2.0 | ✅ |
| celery[redis] | 5.4.0 | BSD-3-Clause | ✅ |
| redis (Python client) | 5.1.1 | MIT | ✅ |
| docker (Python SDK) | 7.1.0 | Apache 2.0 | ✅ |
| robotframework | 7.1 | Apache 2.0 | ✅ |
| robotframework-browser | 18.8.1 | Apache 2.0 | ✅ |
| robotframework-requests | 0.9.7 | Apache 2.0 | ✅ |
| robotframework-databaselibrary | 2.1.3 | Apache 2.0 | ✅ |
| robotframework-appiumlibrary | 2.1.0 | Apache 2.0 | ✅ |

### 2.2 Robot Framework Runner Image
來源：[`backend/Dockerfile.runner`](backend/Dockerfile.runner)

**Base image**：`ppodgorsek/robot-framework:latest`（Apache 2.0；含 Fedora 42 / Python 3.13 / RF 7.3 / Playwright + Chromium / ffmpeg / Node.js）

**Runner image 內額外 pip install**：

| 套件 | 版本 | 授權 |
|---|---|---|
| boto3 | 1.35.36 | Apache 2.0 |
| redis | 5.1.1 | MIT |
| pymysql | 1.1.1 | MIT |
| sqlalchemy | 2.0.35 | MIT |
| pydantic-settings | 2.5.2 | MIT |
| aiofiles | 24.1.0 | Apache 2.0 |
| fastapi[standard] | 0.115.0 | MIT |
| Pillow | 11.0.0 | HPND（Pillow 自己的 fork） |
| numpy | 2.1.3 | BSD-3-Clause |

**System packages**（runner image 內）：
- `xorg-x11-server-Xvfb` — MIT/X11
- `procps-ng` — GPL-2.0+（**僅執行檔，不影響你的程式碼**）

### 2.3 Frontend CDN Scripts
來源：[`frontend/index.html`](frontend/index.html#L7-L14)

| 資源 | 版本 / Tag | 授權 | SaaS 商用 |
|---|---|---|---|
| Tailwind CSS（CDN） | latest（未版本化） | MIT | ✅ |
| Chart.js | latest（未版本化） | MIT | ✅ |
| chartjs-plugin-datalabels | 2.2.0 | MIT | ✅ |
| html2pdf.js | 0.10.1 | MIT | ✅ |
| Font Awesome | 6.4.0 Free | CC BY 4.0（icons） + MIT（CSS/JS） | ⚠️ 須 attribution |

> 💡 **建議**：CDN 的 `latest` tag 對商用而言不穩定，建議 pin 到具體版本（如 `tailwindcss@3.4.0` / `chart.js@4.4.0`），日後升級可控。

### 2.4 Infrastructure Docker Images
來源：[`docker-compose.yml`](docker-compose.yml)

| Service | Image / 版本 | 授權 | SaaS 商用 |
|---|---|---|---|
| postgres | `postgres:16-alpine` | **PostgreSQL License**（類 MIT） | ✅ |
| valkey | `valkey/valkey:8-alpine` | **BSD-3-Clause** | ✅ |
| seaweedfs | `chrislusf/seaweedfs:3.80` | **Apache 2.0** | ✅ |
| seaweedfs-init | `amazon/aws-cli:2.18.5` | Apache 2.0 | ✅ |
| frontend | `nginx:1.27-alpine` | BSD-2-Clause（nginx）+ MIT（Alpine 套件群） | ✅ |
| apisix | `apache/apisix:3.11.0-debian` | **Apache 2.0** | ✅ |
| victoria-logs | `victoriametrics/victoria-logs:v1.50.0` | **Apache 2.0** | ✅ |
| fluent-bit | `fluent/fluent-bit:3.2` | **Apache 2.0** | ✅ |

### 2.5 Backend / Celery Container Base Images

| Dockerfile | Base Image | 授權 | SaaS 商用 |
|---|---|---|---|
| [`backend/Dockerfile`](backend/Dockerfile) | `python:3.11-slim` | PSF License + Debian 各套件 | ✅ |
| [`backend/Dockerfile.celery`](backend/Dockerfile.celery) | `mcr.microsoft.com/playwright/python:v1.47.0-jammy` | Apache 2.0 + Ubuntu 各套件 | ✅ |
| [`Dockerfile.bundle`](Dockerfile.bundle) | `ubuntu:24.04` | 各套件混合（Free Software） | ✅ |

### 2.6 System Tools 與背景元件

| 元件 | 用途 | 授權 |
|---|---|---|
| Playwright | WEB 自動化（Browser Library 底層） | Apache 2.0 |
| Chromium | 瀏覽器引擎（Playwright 內建） | BSD + LGPL（runtime 用，無散佈義務） |
| ffmpeg | 影片處理（測試錄影） | LGPL 2.1+（**重要**：Docker image 內僅執行檔，不修改 ffmpeg 源碼即可商用）|
| Xvfb | 虛擬 X server | MIT/X11 |
| Node.js | rfbrowser init 用 | MIT + 各模組 |
| Docker CLI / Compose plugin | 容器管理 | Apache 2.0 |

---

## ⚠️ 需要特別注意的元件（剩 1 個 + 1 個歷史紀錄）

### 3.1 Font Awesome 6.4.0 Free — CC BY 4.0 + MIT

**授權拆解**：
- **Icons (SVG / glyph)**：CC BY 4.0
- **CSS / JS**：MIT
- **Pro 版（Sharp、Duotone 等）**：商業訂閱，本平台沒用到

**CC BY 4.0 唯一義務**：保留 attribution（在使用 icon 的網站某處註明使用了 Font Awesome）。

**SaaS 場景對你的影響**：

| 動作 | 是否合規 |
|---|---|
| ✅ 在前端 UI 使用 Font Awesome icons | OK |
| ⚠️ 在 footer / about 頁加上 "Icons by Font Awesome" 註腳 | **須做** |
| ❌ 把 icons 修改後當成自己的圖示集賣 | 須附上原 attribution |

**建議改動**（SaaS 上線前）：在 [`frontend/index.html`](frontend/index.html) 的 footer 區或登入頁加一行：

```html
<p class="text-[10px] text-stone-400">
  Icons by <a href="https://fontawesome.com" target="_blank" rel="noopener">Font Awesome</a>
  (CC BY 4.0 · Free version)
</p>
```

### 3.2 歷史變更紀錄（已棄用元件）

下列元件的 Copyleft / 受限授權**曾經**是 SaaS 部署的疑慮，本版本已全部替換：

| 已替換掉 | 替換為 | 受限原因 → 替換後 |
|---|---|---|
| MySQL 8.0（GPLv2 + FOSS Exception） | **PostgreSQL 16-alpine** | GPL 在純 SaaS 場景已合規，但 PostgreSQL License 更乾淨、無條件商用 |
| Redis 7.4+（SSPLv1 / RSALv2） | **Valkey 8-alpine** | SSPL 不可把 Redis 當 managed cache 賣；Valkey 為 BSD-3，無此限制 |
| MinIO（AGPLv3） | **SeaweedFS 3.80** | AGPL 修改源碼後須公開；SeaweedFS 為 Apache 2.0，完全自由 |

> 💡 替換後 backend 程式碼**幾乎沒動**：
> - PostgreSQL：DB driver 從 aiomysql/pymysql 換成 asyncpg/psycopg；DATABASE_URL scheme 換成 postgresql+asyncpg
> - Valkey：與 Redis wire protocol 100% 相容，redis-py 與 celery[redis] 都不變
> - SeaweedFS：S3 API 相容，boto3 與所有 endpoint URL 邏輯都不變（只是端口從 9000 → 8333）

---

## 🛡️ SaaS 部署的合規建議

### Docker Desktop vs Docker Engine — 一定要分清楚

| 場景 | 用什麼 | 授權 | 費用 |
|---|---|---|---|
| 開發機（Windows / macOS） | **Docker Desktop** | Docker Subscription Service Agreement | ⚠️ 員工 > 250 **或** 年營收 > $10M USD 須訂閱（Pro / Team / Business） |
| 生產 / SaaS 雲端伺服器（Linux） | **Docker Engine + Compose plugin** | Apache 2.0 | ✅ **完全免費** |

**SaaS 部署建議**：

1. 雲端正式環境（AWS / GCP / Azure / 自架機房 Linux server）→ 一律用 Docker Engine，零授權費
2. 開發團隊若 < 250 人且年營收 < $10M → Docker Desktop 也免費
3. 大型企業 → 替每位開發者買 Docker Pro 訂閱（約 $5/人/月），或請開發者改用 [Rancher Desktop](https://rancherdesktop.io/) / [Podman Desktop](https://podman-desktop.io/)（兩者都是 Apache 2.0、零授權）

### SaaS 上線前的合規 Checklist

- [ ] **不修改** MinIO / Redis / MySQL 源碼（只用官方 image）
- [ ] **不對外暴露** MinIO API / Redis port（防火牆只開 80/443）
- [ ] Font Awesome **attribution** 加到 footer
- [ ] Repo 根目錄保留 `LICENSES.md`（本檔）
- [ ] 在 SaaS 平台的「關於 / 法律聲明」頁列出主要相依（FastAPI / Robot Framework / Playwright / ...）
- [ ] 雲端正式環境用 Linux server + Docker Engine（非 Docker Desktop）
- [ ] 大型企業內部開發若用 Docker Desktop 須訂閱；個人 / 小團隊免費
- [ ] **正式商用前由法務 review** 本文件 + 實際使用情境

### 已採用的「乾淨」資料層 + 平台層（v1.0 預設）

整個 stack 已改為 100% Apache 2.0 / BSD-3 / PostgreSQL License / MIT，無任何 Copyleft 風險：

| 角色 | 元件 | 授權 |
|---|---|---|
| 關聯式資料庫 | **PostgreSQL 16-alpine** | PostgreSQL License（類 MIT） |
| 快取 / Celery broker | **Valkey 8-alpine** | BSD-3-Clause |
| 物件儲存（S3 相容） | **SeaweedFS 3.80** | Apache 2.0 |
| HTTP server / 靜態頁 | **nginx 1.27-alpine** | BSD-2-Clause |
| API 閘道器 | **Apache APISIX 3.11** | Apache 2.0 |
| 日誌採集 | **Fluent Bit 3.2** | Apache 2.0 |
| 日誌儲存 + 面板（vmui） | **VictoriaLogs 1.6** | Apache 2.0 |

---

## 📚 參考連結

- **MinIO License**: https://github.com/minio/minio/blob/master/LICENSE
- **Redis License**: https://redis.io/legal/licenses/
- **MySQL FOSS Exception**: https://www.mysql.com/about/legal/licensing/foss-exception/
- **Font Awesome Free License**: https://fontawesome.com/license/free
- **Docker Subscription**: https://www.docker.com/pricing/
- **OSI Approved Licenses**: https://opensource.org/licenses/
- **Apache License 2.0**: https://www.apache.org/licenses/LICENSE-2.0
- **AGPL v3**: https://www.gnu.org/licenses/agpl-3.0.html
- **SSPL FAQ (MongoDB 原作)**: https://www.mongodb.com/licensing/server-side-public-license/faq

---

<p align="center">
<sub>本文件為技術整理，非法律意見。授權條款與商業條款隨時可能變動，正式商用前請以對應官方網站最新版本為準。</sub>
</p>
