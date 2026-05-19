# 備份還原演習 SOP

定期驗證備份流程確實能產出可還原的快照。略過演習是讓團隊在六個月後——通常是最需要備份的時候——才發現備份早已損壞的最常見原因。

---

## 執行頻率

- **每季一次**（在 staging 環境）：完整 down + restore + 冒煙測試。
- **每次重大異動後**：PostgreSQL 版本升級、SeaweedFS 佈局變更、影響資料結構的 Alembic migration、`.env` schema 異動。
- **正式還原前**：絕對不要在沒有先在其他地方測試還原過的情況下，直接對生產環境還原快照。

---

## 演習範圍

本演習對 staging 環境（與生產環境設定相同）端對端執行 `scripts/backup.sh` 和 `scripts/restore.sh`。

演習**不**涵蓋以下項目：

- 跨區域災難復原（如已啟用 S3 鏡像，請另行執行）
- 個別資料表的局部還原（可臨時使用 `pg_restore -t`）
- 災難復原的 RTO 目標（透過專屬容量測試追蹤）

---

## 步驟

### 步驟一：建立快照

備份有兩個入口，產出的檔案數不同 — 演習請優先使用 host 端完整版（5 個檔），以驗證 `.env` 加密與 manifest 流程：

| 入口 | 執行方式 | 產出檔案 |
|---|---|---|
| **Host 端完整版**（演習用）| `BACKUP_KEY_FILE=~/.config/autotest/backup.key ./scripts/backup.sh` | `postgres.dump.gz`、`seaweedfs.tar.gz`、`env.enc`、`manifest.json`、`SHA256SUMS` |
| **容器內精簡版**（每日 cron）| `docker exec autotest-backup-cron sh /backup.sh` | `postgres.dump.gz`、`seaweedfs.tar.gz`、`SHA256SUMS`（無 `.env` 加密、無 manifest） |

> 容器版（`scripts/container-backup.sh`）為日常自動快照，因容器內無 `.env` 與 `BACKUP_KEY_FILE`，刻意不加密 `.env` 也不寫 manifest；正式演習與災難復原請以 host 版為準。

### 步驟二：完全拆除 stack

```bash
docker compose down -v
```

`-v` 是**刻意的**——刪除 volume 才能讓還原真正重建狀態。跳過此步驟的演習毫無意義。

### 步驟三：重新啟動乾淨的 stack

```bash
docker compose up -d postgres valkey seaweedfs
```

等待 healthcheck 全部變綠。

### 步驟四：對快照執行還原

```bash
./scripts/restore.sh ./backups/<timestamp>/
```

注意 `SHA256SUMS` 驗證行——若驗證失敗，問題出在快照本身，而非還原流程。

### 步驟五：冒煙測試

開啟瀏覽器，用已知帳號登入，打開一個專案，執行一個測試案例，查看一份報告。若任何步驟失敗，演習即告失敗；記錄問題後再進行修復。

### 步驟六：記錄結果

在 [docs/ops/backup-drill-history.md](backup-drill-history.md) 新增一筆記錄（僅追加）。
包含：日期、執行者、快照 tag、冒煙測試結果、還原時間。

---

## 常見失敗模式

| 症狀 | 可能原因 | 處理方式 |
|---|---|---|
| `SHA256SUMS` 不符 | 快照在靜止狀態下損壞；常見原因為不穩定的網路儲存目的地 | 改為本機先備份再 rsync 的流程 |
| `pg_restore` 回報 ownership 錯誤 | script 已加 `--no-owner --no-privileges`；若仍失敗則 dump 來自不同 PostgreSQL 大版本 | 確認 dump 與 restore 的 PG 主版本一致 |
| SeaweedFS volume 還原後為空 | tarball 備份了錯誤目錄 | 確認 `SEAWEED_DATA_DIR` 環境變數與容器實際目錄一致 |
| `alembic upgrade head` 失敗 | dump 的 schema 來自比目前部署更新的程式碼版本 | 在重試前將部署版本與 dump 的版本對齊 |

---

## 還原時間預算（參考值）

以下為參考硬體規格（4 vCPU / 16 GB RAM / PostgreSQL 50 GB / SeaweedFS 20 GB）的完整演習時間：

| 步驟 | 時間 |
|---|---|
| 備份 | 約 3 分鐘 |
| `docker compose down -v` + `up -d` | 約 1 分鐘 |
| `pg_restore` | 約 5 分鐘 |
| SeaweedFS 解壓縮 | 約 2 分鐘 |
| 冒煙測試 | 約 1 分鐘 |
| **合計** | **約 12 分鐘** |

資料量遠大於上述規格的生產環境，還原時間應等比例增加；請在每次季度演習時重新評估此表。
