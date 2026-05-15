# 資料安全：備份機制 & 安全 Rebuild 指南

## TL;DR — 哪些指令安全、哪些危險

| 操作 | 指令 | 資料是否保留 |
|---|---|---|
| 重建 image | `docker compose up -d --build` | ✅ 保留 |
| 停止服務 | `docker compose down` | ✅ 保留 |
| 重啟單一容器 | `docker compose restart backend` | ✅ 保留 |
| **清除全部資料** | `docker compose down -v` | ❌ 刪除所有 volume |
| **刪除 volume** | `docker volume rm rl_tmp_postgres_data` | ❌ 資料消失 |

> **重點**：Named volume（`rl_tmp_postgres_data`、`rl_tmp_seaweedfs_data`）只要沒有 `-v` 旗標，任何 rebuild 或重啟都不會影響資料。

---

## 資料持久化架構

```
docker compose up -d --build
        │
        ├─ rebuilds images   (Dockerfile, source code)  ← 每次都重建
        │
        ├─ keeps volumes     (postgres_data, seaweedfs_data)  ← 永遠保留
        │
        └─ keeps bind-mounts (frontend/nginx.conf, frontend/index.html)  ← host 檔案
```

### Named Volumes（持久化資料）

| Volume | 掛載點 | 存什麼 |
|---|---|---|
| `rl_tmp_postgres_data` | `/var/lib/postgresql/data` | 所有資料庫（TestCase、Report、User、RBAC…） |
| `rl_tmp_seaweedfs_data` | `/data` | 截圖、MP4 影片、Playwright trace、測試報告 |

### Bind Mounts（程式碼，不是資料）

| Host 路徑 | 容器路徑 | 說明 |
|---|---|---|
| `~/RL_TMP/frontend/index.html` | `/usr/share/nginx/html/index.html` | SPA 前端（git 版控） |
| `~/RL_TMP/frontend/nginx.conf` | `/etc/nginx/conf.d/default.conf` | nginx 設定（git 版控） |

---

## 備份機制

### 備份內容

```
backups/
└── 20260515-030000/          ← YYYYMMDD-HHMMSS
    ├── postgres.dump.gz       pg_dump --format=custom（可用 pg_restore 還原）
    ├── seaweedfs.tar.gz       SeaweedFS /data 完整打包
    ├── env.enc                .env AES-256-CBC 加密（需 BACKUP_KEY_FILE）
    ├── manifest.json          版本、git hash、時間戳
    └── SHA256SUMS             完整性校驗
```

### 執行備份

```bash
# 手動備份（寫入 ./backups/<timestamp>/）
cd ~/RL_TMP
./scripts/backup.sh

# 指定目的地 + 保留天數
BACKUP_DEST=/srv/backups BACKUP_KEEP_DAYS=14 ./scripts/backup.sh
```

### 設定每日自動備份（Cron）

```bash
# 安裝 cron job（每天 03:00，保留 7 天）
cd ~/RL_TMP
./scripts/setup-cron.sh

# 自訂目的地與保留天數
BACKUP_DEST=/srv/backups BACKUP_KEEP_DAYS=14 ./scripts/setup-cron.sh

# 確認已安裝
crontab -l | grep autotest-backup

# 查看備份 log
tail -f ~/RL_TMP/backups/cron.log

# 移除 cron job
crontab -l | grep -v 'autotest-backup' | crontab -
```

### 備份保留政策

`backup.sh` 結尾會自動清理超過 `BACKUP_KEEP_DAYS`（預設 7 天）的舊 snapshot 目錄。  
命名格式為 `YYYYMMDD-HHMMSS`，只清符合此格式的目錄，不會誤刪其他檔案。

---

## 安全 Rebuild 流程

```bash
# 標準安全 rebuild（會先自動備份，再 rebuild image）
cd ~/RL_TMP
./scripts/safe-rebuild.sh

# 跳過備份（CI 環境、快速測試用）
./scripts/safe-rebuild.sh --no-backup
```

`safe-rebuild.sh` 執行順序：
1. 執行 `backup.sh` 建立快照（備份失敗即中止）
2. 執行 `docker compose up -d --build`（**沒有** `-v`，volumes 完全保留）
3. 等待 postgres / valkey / seaweedfs / backend 全部 healthy（最多 120 秒）
4. curl `/api/healthz` 確認服務正常

---

## 還原流程

```bash
# 列出可用快照
ls ~/RL_TMP/backups/

# 還原指定快照（會停 backend + celery，重建 DB，再起動）
cd ~/RL_TMP
./scripts/restore.sh ./backups/20260515-030000
```

還原後檢查：
```bash
docker compose ps
curl http://localhost/api/healthz
docker compose logs --tail=50 backend
```

---

## 季度演習 SOP

詳見 [backup-drill.md](backup-drill.md)。核心步驟：
1. `./scripts/backup.sh` → 確認 5 個檔案都存在
2. `docker compose down -v` → 完全清空
3. `docker compose up -d postgres valkey seaweedfs` → 等 healthy
4. `./scripts/restore.sh ./backups/<ts>` → 還原
5. 開瀏覽器 → 登入 → 打開一個 TestCase → 執行 → 看報告

---

## 常見問題

**Q：我改了 frontend/index.html，rebuild 後會不見嗎？**  
不會。`index.html` 是 bind-mount 自 host `~/RL_TMP/frontend/index.html`，rebuild 不影響 host 檔案。

**Q：不小心跑了 `docker compose down`（沒有 `-v`），資料還在嗎？**  
還在。`down` 只停容器，named volume 不動。`docker compose up -d` 即可恢復。

**Q：`docker compose down -v` 已經跑了，怎麼辦？**  
立刻從最新的 backup snapshot 還原：`./scripts/restore.sh ./backups/<最新目錄>`。

**Q：備份跑多久？**  
在 4vCPU/16GB 主機上，pg_dump ~1分鐘，seaweedfs tar 視容量而定（20GB ≈ 2分鐘）。

**Q：備份期間服務會停嗎？**  
不會。`pg_dump` 和 `seaweedfs tar` 都是 online 備份，使用者不會感覺到服務中斷。
