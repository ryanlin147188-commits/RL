# First-admin bootstrap (operator runbook)

A fresh AutoTest deployment **automatically seeds a default admin** on first
backend start. There is no longer a self-service registration path or invite
mint endpoint — every account in the system is created by an admin.

## TL;DR — first login

```
帳號:   admin
密碼:   admin123
```

The first time `admin` logs in, the backend forces a password rotation:
the API returns `must_change_password=true` on `/auth/login`, and every
non-`/auth/me` / non-`/auth/change-password` endpoint returns `403`
until the password is rotated. The frontend pops a forced-change modal
that blocks all other UI.

## How the seed works

`backend/app/main.py::_ensure_default_admin()` runs in the lifespan
startup hook. On every backend boot:

1. If a `users` row with `username='admin'` **already exists**, the seed
   self-heals (`is_superuser=True`, `is_active=True`, `role_id=Admin`)
   but **does not touch `password_hash` or `must_change_password`** — so
   restarts never reset the operator-set password.
2. If `admin` is missing, the seed creates it with:
   - `password_hash = hash_password(AUTOTEST_DEFAULT_ADMIN_PASSWORD or 'admin123')`
   - `must_change_password = True`
   - `is_superuser = True`
   - `role = Admin`
   - `organization = default`

### Customising the seed password (prod)

`admin123` is a known-bad default. For prod / staging, set the
`AUTOTEST_DEFAULT_ADMIN_PASSWORD` env var **before** the very first
boot. The seed will use that string instead of `admin123`. Either way,
`must_change_password=True` so the operator still has to rotate on
first login.

```sh
# .env(務必在 backend 容器第一次啟動之前設好)
echo "AUTOTEST_DEFAULT_ADMIN_PASSWORD=Op3rat0r-S0lid-Initial" >> .env

docker compose up -d backend
```

## Adding more admins / users

After logging in as `admin`, go to **設定 → 專案協作成員** to:

- 「**建立新使用者**」(綠色按鈕)— 建立全新帳號(username + email + 初始密碼 + 角色),可選擇同時加入當前專案
- 「**編輯使用者**」(每列 ✎ 按鈕)— 改 display_name / email / 角色 / 啟用旗標 / superuser 旗標
- 「**重設密碼**」(編輯 modal 內)— 設新密碼,目標帳號的 `must_change_password` 會自動設回 `True`,他下次登入會被強制再改一次
- 「**徹底刪除帳號**」(每列 ✗ 按鈕)— cascade 清掉 ProjectMember 等關聯

Programmatic equivalent:

```sh
# 建立新使用者(superuser only)
curl -X POST http://localhost/api/auth/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "password": "alice-initial",
    "email": "alice@example.com",
    "is_superuser": false
  }'
```

## 忘記密碼

登入頁的「**忘記密碼**」 tab → 輸入 username + email → 系統寄重置連結到該 email
(連結 1 小時內有效,單次使用)→ 點連結進設定新密碼頁,完成後自動重新登入。

需要 SMTP 設定(設定 → 電子郵件)才能真的寄信;沒設 SMTP 時 token 仍會
建到 `password_reset_tokens` 表,可從 DB 取出測試:

```sh
docker exec autotest-postgres psql -U admin -d autotest_db -c \
  "SELECT token, expires_at FROM password_reset_tokens \
   WHERE username='admin' ORDER BY created_at DESC LIMIT 1;"
```

把 token 拼到 `http://localhost/?reset_token=<TOKEN>` 即可。

## What if I lose admin password and the SMTP isn't set?

```sh
# 直接重設 admin 的 password_hash
HASH=$(docker exec autotest-backend python -c \
  "from app.auth.security import hash_password; print(hash_password('admin123'))")

docker exec autotest-postgres psql -U admin -d autotest_db -c \
  "UPDATE users SET password_hash='$HASH', must_change_password=true \
   WHERE username='admin';"

# 然後用 admin / admin123 登入,系統強制改新密碼
```

## 已下架的舊機制(歷史紀錄)

下列 endpoint / 流程在 0008+0009 後已全部移除,跑舊文件 / 舊 client 的話會碰到:

| 舊 endpoint / 流程 | 新行為 |
|---|---|
| `POST /api/auth/register` | `410 Gone` + `code=registration_disabled` |
| `POST /api/auth/bootstrap-invite` | 完全移除(原本用來 mint 第一張 invite) |
| `POST /api/auth/redeem-invite` | 完全移除 |
| `POST /api/auth/request-access` | 完全移除 |
| Email-domain 自動歸屬 | 邏輯刪除;`organizations.email_domains` 欄位保留但 API 不再讀寫 |
| 邀請碼管理 UI | 從設定頁拿掉 |
| 組織成員 / 群組設定 UI | 從設定頁拿掉(群組 model 仍在,給「指派 todo 給群組」共用) |
| `python -m app.cli create-admin` | CLI 仍可用,但不再是「首次部署必跑」 — 系統會自動 seed |

`AUTOTEST_BOOTSTRAP_TOKEN` env var 已不再被 backend 讀取 — 設了也不會啟用任何流程。
