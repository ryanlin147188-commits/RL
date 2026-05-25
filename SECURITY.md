# 安全政策

## 支援版本

安全修補程式套用於 `main` 分支上的最新次要版本。舊版本不再維護。

| 版本 | 支援狀態 |
|---|---|
| 1.1.9（最新） | ✅ 支援 |
| 1.1.x | ✅ 支援 |
| 1.0.x | ⚠️ 僅修補 CVE-9.0+ |
| < 1.0 | ❌ 不支援 |

---

## 回報安全漏洞

**請勿針對安全漏洞開立公開的 GitHub Issue。**

如果您發現 AutoTest 的安全漏洞，請透過以下管道私下回報：

1. **GitHub Security Advisory（建議）**：
   在此開立私人 Advisory：<https://github.com/ryanlin147188-commits/RL-for-Kapito/security/advisories/new>
2. **電子郵件**：<ryanlin147188@gmail.com>
   主旨請使用：`[AutoTest Security] <簡短標題>`

回報時請提供：

- 漏洞說明及其潛在影響
- 重現步驟或概念驗證
- 受影響的版本或 commit hash
- 您建議的修復方式（如有）
- 您是否打算公開披露，以及預計時間表

---

## 我們的回應流程

- 我們將在 **72 小時**內確認收到回報。
- 我們的目標是在 **7 天**內提供初步評估。
- 對於確認的漏洞，我們將與您協調披露時間表（視嚴重程度通常為 30–90 天）。
- 除非您要求匿名，我們將在版本說明中列出您的貢獻。

---

## 不在範圍內的項目

以下情況通常**不**視為安全漏洞：

- 需要實體存取使用者裝置的問題
- 已公開披露的第三方相依漏洞（請優先向上游回報）
- 需要受害者自行將攻擊者控制的內容貼到瀏覽器 console 的 Self-XSS
- 無法展示實際影響的缺少安全標頭
- 預設憑證或本機開發設定（正式部署應覆蓋這些設定，詳見 [README.md](README.md)）

---

## 自架部署的安全強化建議

在正式環境執行 AutoTest 時，請確保：

- `AUTOTEST_JWT_SECRET` 和 `AUTOTEST_FERNET_KEY` 設為足夠長的隨機值（部署腳本在缺少時會自動產生）。
- `ALLOWED_ORIGINS` 設為你的實際前端 origin，**絕對不要**使用 `*`。
- 資料庫、S3 和管理員帳號憑證已從預設值輪替。
- 服務部署在 HTTPS 後方（例如搭配有效 TLS 憑證的 reverse proxy）。
- 容器 image 釘定為特定版本或 digest（含動態 spawn 的 `RECORDER_IMAGE` / `ROBOT_RUNNER_IMAGE`），**不使用** `latest`。
- PostgreSQL volume 和 SeaweedFS volume 有定期備份排程（內建 `backup-cron` 容器已負責每日 03:00 自動備份）。
- 正式環境使用 **Docker Engine**（免費，Apache 2.0），而非 Docker Desktop（超過 250 人或年營收 > $10M USD 的企業需付費訂閱）。
- **不要執行 `docker image prune -a`** — 會把目前沒 running 的 `autotest-robot-runner` / `autotest-recorder` 砍掉，下次執行 / 錄製會失敗，必須重新 build。安全清理改用 `docker container/volume/image prune -f` + `docker builder prune -af`。

**Docker Log 輪替（一次性設定）：**

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

---

## v1.1.5 — in-process authlib OIDC 安全強化

- **`ZOHO_CLIENT_SECRET` 只能放 `.env` / secret manager**，絕對不要 commit 進版本庫。
  Backend 直接將其放在 process env；authlib 不會將其寫入 DB。
  輪替密鑰時修改 `.env` 後執行 `docker compose up -d --force-recreate backend` 即生效。
- **state cookie 使用 HS256 + `AUTOTEST_JWT_SECRET` 簽章**，TTL 10 分鐘、`Path=/api/auth`、`HttpOnly`、`SameSite=Lax`。
  OAuth state CSRF 防護不依賴外部 session store，即使沒有 `SessionMiddleware` 也能正常運作。
- **未設 email 限制時**，任何 Zoho 帳號都能透過 JIT 進入系統。
  新使用者的 `role_id=NULL` 且沒有任何 `project_members`，Casbin enforce 對所有專案級端點都會拒絕，使用者只能讀取 `/api/auth/me`，需由管理員在「設定 → 專案協作成員」分配專案與角色後才能使用。
  如需更嚴格的管控，可在 [backend/app/auth/oidc.py](backend/app/auth/oidc.py) 的 `normalize_claims()` 或 `_provision_from_claims()` 加入 email domain 檢查（一行 if 即可）。

---

## Self-Service 邀請流程

端點 `POST /api/auth/request-access` 和 `GET /api/organizations/by-email-domain` 為**匿名**端點（不需要 Authorization header），以便讓潛在使用者在沒有帳號的情況下請求存取。這擴大了公開攻擊面；請注意：

- 將每個 `Organization.email_domains` 設為**你實際控制**的網域（例如 `acme.com`，而非 `gmail.com`）。任何 email domain 符合的地址都會收到該組織的 Viewer 角色邀請。
- 相同 domain 不能同時被兩個組織認領。如果存在重複，migration `0004_assignment_invite_email` 會記錄警告；在公開此端點之前請先清除重複資料。
- 內建速率限制為**每 IP 每小時 5 次**，加上**每 email 60 秒冷卻**。如果你的前端位於 CDN 或負載平衡器後方，請正確設定 `X-Forwarded-For` 信任，確保 slowapi 看到真實的 client IP。
- 邀請 token **不會**在 HTTP 回應中返回；只會透過 email 寄送。請確保已設定 SMTP（設定 → 電子郵件），否則使用者不會收到任何通知。
- 邀請與請求的 email 綁定，有效期 24 小時，且只能使用一次。

---

## 電子郵件通知

`notify(...)` 無條件寫入一筆 `Notification` 資料列，並**僅在**收件者的 `NotificationPreference[event_key].email == True` 時才將 email 加入佇列。SMTP 憑證以 Fernet 加密儲存在 `EmailConfig` 中；輪替 `AUTOTEST_FERNET_KEY` 時請謹慎——遺失金鑰將導致現有資料列無法讀取。

---

感謝您協助保護 AutoTest 及其使用者的安全。
