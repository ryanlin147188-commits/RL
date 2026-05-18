# 首次部署操作手冊

全新部署的 AutoTest 會在 backend 第一次啟動時**自動建立預設管理員帳號**。系統沒有自助註冊路徑——系統中的每個帳號都由管理員建立。

---

## 快速開始（v1.1.5+）

```
帳號：admin
密碼：admin123
```

`admin` 第一次登入時，backend 會強制執行密碼輪替：`/auth/login` 回傳 `must_change_password=true`，在密碼輪替完成之前，所有非 `/auth/me` / `/auth/change-password` 的端點都會回傳 `403`。前端會彈出強制修改視窗，鎖定所有其他 UI 操作。

---

## 自動產生 .env 密鑰

全新部署時，執行 bootstrap service 一次性產生 `.env`：

```bash
docker compose --profile init run --rm bootstrap
```

bootstrap 會自動產生以下隨機密鑰，寫入 `.env`：

| 變數 | 說明 |
|---|---|
| `DB_PASSWORD` | PostgreSQL 資料庫密碼 |
| `S3_ROOT_PASSWORD` | SeaweedFS S3 存取密碼 |
| `AUTOTEST_JWT_SECRET` | JWT 簽章密鑰 |
| `AUTOTEST_FERNET_KEY` | Fernet 對稱加密金鑰（Email/Token 加密） |
| `REPLICA_PASSWORD` | PostgreSQL streaming replication 使用者密碼 |

> **重要**：`.env` 包含所有密鑰，請勿 commit 進版本庫，並設定適當的檔案權限（`chmod 600 .env`）。

---

## 正式環境自訂初始密碼

`admin123` 是已知的不安全預設值。正式或 staging 環境建議在第一次啟動 backend **之前**設定自訂初始密碼：

```bash
# .env（務必在 backend 容器第一次啟動之前設好）
echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rat0r-S0lid-Initial" >> .env

docker compose up -d backend
```

無論使用何種密碼，`must_change_password=true` 都會讓操作員在首次登入時被強制再次修改。

---

## 啟用 Zoho OIDC SSO（選用）

v1.1.5 起 OIDC 採用 in-process `authlib`，不需要 Casdoor sidecar，設好 `.env` 即生效：

```bash
# 步驟 1：前往 https://api-console.zoho.com
#         → Add Client → Server-based Applications
#         Authorized Redirect URIs: http://<主機>/api/auth/zoho/callback

# 步驟 2：將 client_id / secret 寫入 .env
echo "ZOHO_CLIENT_ID=<id>"                                       >> .env
echo "ZOHO_CLIENT_SECRET=<secret>"                               >> .env
echo "ZOHO_REDIRECT_URL=http://<主機>/api/auth/zoho/callback"    >> .env

# 步驟 3：重啟 backend
docker compose up -d --force-recreate backend
```

重新整理 SPA 登入頁，橘色「使用 Zoho 登入」按鈕即會出現。

第一次透過 Zoho 登入的使用者會 JIT 建立本地 `users` 資料列（`oidc_provider='zoho'` + `oidc_subject=<Zoho ZUID>`），`role_id=NULL`、沒有任何 `project_members`。管理員在「設定 → 專案協作成員」分配專案與角色後，該使用者才能使用系統功能。

---

## 建立更多管理員 / 使用者

以 `admin` 登入後，前往**設定 → 專案協作成員**：

- **建立新使用者**（綠色按鈕）：建立全新帳號，指定 username + email + 初始密碼 + 角色，可選擇同時加入當前專案。
- **編輯使用者**（每列 ✎ 按鈕）：修改 display_name / email / 角色 / 啟用旗標 / superuser 旗標。
- **重設密碼**（編輯 modal 內）：設定新密碼，目標帳號的 `must_change_password` 會自動設回 `true`，該使用者下次登入時會被強制再次修改。
- **刪除帳號**（每列 ✗ 按鈕）：cascade 清除 `ProjectMember` 等關聯資料。

**API 方式建立使用者（僅 superuser）：**

```bash
curl -X POST http://localhost/api/auth/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "password": "alice-initial",
    "email": "alice@example.com",
    "is_superuser": false
  }'
```

---

## 忘記密碼

1. 登入頁點「忘記密碼」
2. 輸入使用者名稱與 Email
3. 收到重設連結（需管理員已設定 SMTP，見「設定 → 電子郵件」）
4. 點連結進入重設頁，輸入新密碼（至少 6 個字元），完成後自動重新登入

未設定 SMTP 時，token 仍會建立在 `password_reset_tokens` 表中，可從 DB 取出測試：

```bash
docker exec autotest-postgres psql -U admin -d autotest_db -c \
  "SELECT token, expires_at FROM password_reset_tokens \
   WHERE username='admin' ORDER BY created_at DESC LIMIT 1;"
```

將 token 拼到 `http://localhost/?reset_token=<TOKEN>` 即可直接重設。

---

## 遺失 admin 密碼且未設定 SMTP

```bash
# 直接重設 admin 的 password_hash
HASH=$(docker exec autotest-backend python -c \
  "from app.auth.security import hash_password; print(hash_password('admin123'))")

docker exec autotest-postgres psql -U admin -d autotest_db -c \
  "UPDATE users SET password_hash='$HASH', must_change_password=true \
   WHERE username='admin';"

# 然後用 admin / admin123 登入，系統會強制要求設定新密碼
```

---

## HTTPS / 自簽憑證（v1.1.2+）

從 v1.1.2 起，frontend 容器同時監聽 `:80`（HTTP）和 `:443`（HTTPS）。HTTPS 使用 **build-time 產生的自簽憑證**（`CN=autotest-platform`，10 年效期），用於讓 Playwright Trace Viewer 所需的 `SharedArrayBuffer` 和 Service Worker 在 secure context 下可用（瀏覽器規定 SAB / SW 只在 HTTPS 或 `http://localhost` 下啟用）。

### 方式 A：拖放工作流（免安裝憑證，建議）

報告頁的「**Trace Viewer**」按鈕會自動下載 `trace.zip` 並開啟官方 `trace.playwright.dev`。將下載的 `.zip` 拖入 trace.playwright.dev 分頁即可，**完全不需要處理自簽憑證**。

### 方式 B：將自簽憑證加入 OS 信任（一次設定終身使用）

**macOS：**
```bash
curl -o /tmp/autotest.crt http://<主機IP>/install-cert/server.crt && \
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain /tmp/autotest.crt
```

**Linux（Debian / Ubuntu）：**
```bash
sudo curl -o /usr/local/share/ca-certificates/autotest.crt \
  http://<主機IP>/install-cert/server.crt
sudo update-ca-certificates
```

**Windows（PowerShell，系統管理員）：**
```powershell
Invoke-WebRequest -Uri "http://<主機IP>/install-cert/server.crt" `
  -OutFile "$env:TEMP\autotest.crt"
Import-Certificate -FilePath "$env:TEMP\autotest.crt" `
  -CertStoreLocation Cert:\LocalMachine\Root
```

安裝後重啟瀏覽器，連至 `https://<主機IP>/` 不再顯示憑證警告，Trace Viewer 自托管版本可用。

### 正式環境應更換為 CA 簽發憑證

LAN 自簽憑證僅適用於 dev / staging。正式環境建議：

- 使用真實 DNS + Let's Encrypt（若 server 對外可達）
- 內部 PKI 簽發（若公司有 CA）
- 在 nginx 前方套 reverse proxy（Caddy / Traefik）進行 TLS termination

更換憑證時，將新的 cert/key 透過 `volumes:` 覆蓋 image 內的 `/etc/nginx/certs/server.crt` + `server.key` 即可，無需重新建置 image。

---

## 已下架的舊機制（歷史紀錄）

以下端點 / 流程在 migration 0008+0009 後已全部移除：

| 舊端點 / 流程 | 新行為 |
|---|---|
| `POST /api/auth/register` | `410 Gone` + `code=registration_disabled` |
| `POST /api/auth/bootstrap-invite` | 完全移除 |
| `POST /api/auth/redeem-invite` | 完全移除 |
| `POST /api/auth/request-access` | 完全移除 |
| Email-domain 自動歸屬 | 邏輯刪除；`organizations.email_domains` 欄位保留但 API 不再讀寫 |
| 邀請碼管理 UI | 從設定頁移除 |
| `python -m app.cli create-admin` | CLI 仍可用，但不再是「首次部署必跑」——系統會自動 seed |

`AUTOTEST_BOOTSTRAP_TOKEN` 環境變數已不再被 backend 讀取——設定後不會啟用任何流程。
