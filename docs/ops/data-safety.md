# 資料安全：備份機制與安全 Rebuild 指南

> 適用版本：v1.1.9+

## 速查表：哪些操作安全、哪些危險

| 操作 | 指令 | 資料是否保留 | spawn image 是否保留 |
|---|---|---|---|
| 重建 image | `docker compose up -d --build` | ✅ 保留 | ✅ 保留 |
| 停止服務 | `docker compose down` | ✅ 保留 | ✅ 保留 |
| 重啟單一容器 | `docker compose restart backend` | ✅ 保留 | ✅ 保留 |
| 安全孤兒清理 | `docker volume prune -f && docker image prune -f` | ✅ 保留 | ✅ 保留 |
| 清 build cache | `docker builder prune -af` | ✅ 保留 | ✅ 保留 |
| **清除全部資料** | `docker compose down -v` | ❌ 刪除所有 volume | ✅ 保留 |
| **刪除 volume** | `docker volume rm <專案名>_postgres_data` | ❌ 資料消失 | ✅ 保留 |
| **❌ 危險：全 image 清** | `docker image prune -a` | ✅ 保留 | ❌ **`autotest-robot-runner` / `autotest-recorder` 被刪** |

> **重點**：Named volume（`postgres_data`、`seaweedfs_data` 等）只要沒有 `-v` 旗標，任何 rebuild 或重啟都不會影響資料。
>
> **volume 名稱的實際前綴**：docker compose 會自動把 named volume 加上「專案名前綴」（compose 預設取 working dir 名稱小寫，例如 `RL_TMP/` → `rl_tmp_postgres_data`；本機若是 `rl-for-kapito-main/` 則變 `rl-for-kapito-main_postgres_data`）。可用 `docker volume ls` 確認實際名稱，或用 `COMPOSE_PROJECT_NAME` 環境變數固定前綴。
>
> **動態 spawn image**：v1.1.9 起，每次測試執行 / 錄製由 backend 動態 `docker run --rm autotest-robot-runner` 或 `autotest-recorder`，**這些 image 沒寫進 docker compose `services:`**，因此 `docker image prune -a`（會清「沒在 running 容器使用的 image」）會把它們砍掉。釋放磁碟時請使用 `docker image prune -f`（不加 `-a`，只清 dangling）+ `docker builder prune -af`（清 build cache）。

---

## 資料持久化架構

```
docker compose up -d --build
        │
        ├─ 重建 images   (Dockerfile、原始碼)    ← 每次都重建
        │
        ├─ 保留 volumes  (postgres_data、seaweedfs_data、
        │                 postgres_replica_data、backup_data)  ← 永遠保留
        │
        └─ 保留 bind-mounts  (frontend/nginx.conf、frontend/index.html)  ← host 檔案
```

### Named Volumes（持久化資料）

> 表格中的 volume 名稱為 `docker-compose.yml` 內定義；實際 docker volume 會由 compose 加上專案名前綴（例如 `rl_tmp_postgres_data`）。

| Volume | 掛載點 | 存什麼 |
|---|---|---|
| `postgres_data` | `/var/lib/postgresql/data`（主庫） | 所有資料庫（TestCase、Report、User、RBAC…） |
| `seaweedfs_data` | `/data` | 截圖、MP4 影片、Playwright trace、測試報告 |
| `postgres_replica_data` | `/var/lib/postgresql/data`（副本） | 主庫的 streaming replication 熱備副本 |
| `backup_data` | `/backups`（backup-cron 容器內） | 每日自動備份快照（保留 7 天） |

### Bind Mounts（程式碼，非資料）

| Host 路徑 | 容器路徑 | 說明 |
|---|---|---|
| `~/RL_TMP/frontend/index.html` | `/usr/share/nginx/html/index.html` | SPA 前端（git 版控） |
| `~/RL_TMP/frontend/nginx.conf` | `/etc/nginx/conf.d/default.conf` | nginx 設定（git 版控） |

---

## 備份機制

### 備份內容

```
backups/
└── 20260515-030000/              ← YYYYMMDD-HHMMSS
    ├── postgres.dump.gz           pg_dump --format=custom（可用 pg_restore 還原）
    ├── seaweedfs.tar.gz           SeaweedFS /data 完整打包
    ├── env.enc                    .env AES-256-CBC 加密（需 BACKUP_KEY_FILE）
    ├── manifest.json              版本、git hash、時間戳
    └── SHA256SUMS                 完整性校驗
```

### 方式一：Docker 內建自動備份（backup-cron 容器，建議）

`backup-cron` 容器在每日 **03:00（Asia/Taipei）** 自動執行備份。備份從 `postgres-replica` 讀取，不影響主庫效能。備份快照儲存在 `backup_data` Docker volume（預設保留 7 天）。

```bash
# 確認容器運行中
docker ps | grep backup-cron

# 手動觸發一次備份
docker exec autotest-backup-cron sh /backup.sh

# 查看備份清單
docker exec autotest-backup-cron ls -lh /backups/

# 查看 cron 執行 log
docker exec autotest-backup-cron cat /backups/cron.log

# 驗證最新快照完整性
docker exec autotest-backup-cron \
  sh -c "cd /backups/\$(ls /backups | grep -v cron | tail -1) && sha256sum -c SHA256SUMS"
```

調整保留天數：在 `.env` 中設定 `BACKUP_KEEP_DAYS=14`，再重啟容器：

```bash
echo "BACKUP_KEEP_DAYS=14" >> .env
docker compose up -d backup-cron
```

### 方式二：Host 端手動備份腳本

```bash
# 手動備份（寫入 ./backups/<timestamp>/）
cd ~/RL_TMP
./scripts/backup.sh

# 指定目的地 + 保留天數
BACKUP_DEST=/srv/backups BACKUP_KEEP_DAYS=14 ./scripts/backup.sh

# 同時同步到 S3（需 aws CLI）
S3_BUCKET=my-autotest-backups ./scripts/backup.sh
```

### 方式三：Host 端 Cron 自動備份

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

---

## PostgreSQL Streaming Replication 熱備

`postgres-replica` 容器透過 WAL streaming replication 即時同步主庫資料，用於：

1. **備份來源**：`backup-cron` 從副本讀取，避免 pg_dump 影響主庫效能
2. **讀取分流**：可接受讀取查詢，減輕主庫負擔

### 驗證副本同步狀態

```bash
# 確認副本正在 streaming
docker exec autotest-postgres-replica psql -U admin -d autotest_db \
  -c "SELECT status, sender_host, written_lsn FROM pg_stat_wal_receiver;"
# → status = streaming，sender_host = postgres

# 確認副本為 standby 模式（不接受寫入）
docker exec autotest-postgres-replica psql -U admin -d autotest_db \
  -c "SELECT pg_is_in_recovery();"
# → t（true）
```

---

## 安全 Rebuild 流程

```bash
# 標準安全 rebuild（先自動備份，再 rebuild image）
cd ~/RL_TMP
./scripts/safe-rebuild.sh

# 跳過備份（CI 環境、快速測試用）
./scripts/safe-rebuild.sh --no-backup
```

`safe-rebuild.sh` 執行順序：

1. 執行 `backup.sh` 建立快照（備份失敗即中止）
2. 執行 `docker compose up -d --build`（**沒有** `-v`，volumes 完全保留）
3. 等待 postgres / valkey / seaweedfs / backend 全部 healthy（最多 120 秒）
4. `curl /api/healthz` 確認服務正常

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
5. 開瀏覽器 → 登入 → 打開一個 TestCase → 執行 → 查看報告

---

## 常見問題

**Q：我改了 `frontend/index.html`，rebuild 後會不見嗎？**
不會。`index.html` 是 bind-mount 自 host `~/RL_TMP/frontend/index.html`，rebuild 不影響 host 檔案。

**Q：不小心跑了 `docker compose down`（沒有 `-v`），資料還在嗎？**
還在。`down` 只停容器，named volume 不動。`docker compose up -d` 即可恢復。

**Q：`docker compose down -v` 已經跑了，怎麼辦？**
立刻從最新備份快照還原：`./scripts/restore.sh ./backups/<最新目錄>`。若使用 Docker 內建 backup-cron，快照在 `backup_data` volume 中，需先以 `docker volume inspect` 找到實際路徑。

**Q：備份跑多久？**
在 4 vCPU / 16 GB 主機上，pg_dump 約 1 分鐘，seaweedfs tar 視容量而定（20 GB ≈ 2 分鐘）。

**Q：備份期間服務會停嗎？**
不會。`pg_dump`（從 replica）和 SeaweedFS tar 都是 online 備份，使用者不會感覺到服務中斷。

**Q：postgres-replica 掉了，主庫會受影響嗎？**
不會。streaming replication 為非同步模式，副本容器異常不影響主庫的讀寫服務。備份 cron 會在下次執行時重試。
