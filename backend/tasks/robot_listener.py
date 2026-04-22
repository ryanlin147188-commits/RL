"""
Robot Framework Listener v3。

職責：
1. 即時把 keyword/test 事件 publish 到 Redis（給前端 WebSocket）。
2. 收集每一個「STEP marker keyword」前後所夾的真正執行 keyword 的狀態與耗時，
   並依 step_index 累計成一份 JSON，供 robot_runner 在執行結束後讀取。

由 robot_runner 透過 `--listener robot_listener.RTListener:<args>` 注入。

Listener 與測試在同一個 Robot process 中執行，靠下列環境變數通訊：
- AUTOTEST_REDIS_URL   : Redis 連線
- AUTOTEST_LOG_CHANNEL : pub/sub channel（task:{task_id}:logs）
- AUTOTEST_RESULT_PATH : 寫入 step 結果 JSON 的路徑
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import redis

# Marker keyword 名稱（runner 會在 .robot 內以 `Log    AT_STEP idx=N`）
_STEP_MARKER_PREFIX = "AT_STEP idx="


class RTListener:
    """Robot listener API v3"""

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self) -> None:
        self._redis_url = os.environ.get("AUTOTEST_REDIS_URL", "")
        self._channel = os.environ.get("AUTOTEST_LOG_CHANNEL", "")
        self._result_path = os.environ.get("AUTOTEST_RESULT_PATH", "")
        self._screenshot_dir_url = os.environ.get("AUTOTEST_SCREENSHOT_URL_PREFIX", "")

        try:
            self._r = redis.from_url(self._redis_url) if self._redis_url else None
        except Exception:
            self._r = None

        # 當前 step index（由 marker keyword 設定；None = 尚未進入任何 step）
        self._current_idx: int | None = None
        # 所有 step 結果 [{step_index, status, duration_ms, error, pre, post}]
        self._results: list[dict[str, Any]] = []
        # 暫存每個 step 的累積資訊
        self._buffer: dict[int, dict[str, Any]] = {}
        # 偵測 Take Screenshot 後的檔案路徑
        self._last_screenshot_path: str | None = None

    # ── 工具 ──────────────────────────────────────────────
    def _publish(self, level: str, message: str) -> None:
        if not self._r or not self._channel:
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self._r.publish(
                self._channel,
                json.dumps({"type": "log", "level": level, "message": f"[{ts}] {message}"}),
            )
        except Exception:
            pass

    def _ensure_buf(self, idx: int) -> dict[str, Any]:
        if idx not in self._buffer:
            self._buffer[idx] = {
                "step_index": idx,
                # 預設 SKIPPED；只有實際執行過 action keyword 才會升級為 PASSED/FAILED。
                # 這樣前一步 FAIL 後 RF 仍會 fire start/end_keyword（status=NOT RUN）給 listener，
                # 我們不會把那些步驟錯誤標成 PASSED。
                "status": "SKIPPED",
                "duration_ms": 0,
                "error": None,
                "pre": None,
                "post": None,
                "_screenshots": [],
                "_action_dur": 0,
            }
        return self._buffer[idx]

    # ── Robot listener hooks ────────────────────────────
    def start_suite(self, data, result):
        self._publish("INFO", f"📁 Suite 開始: {data.name}")

    def start_test(self, data, result):
        # 進入新 test：重置 step 游標，避免上一個 test 的尾段事件污染
        self._current_idx = None
        self._publish("INFO", f"  ▶ Test: {data.name}")

    def end_test(self, data, result):
        # test 結束後 [Teardown] 的 keyword 仍會 fire；要立刻關掉游標，
        # 否則 teardown 內的 PASS 會被誤算到最後一個 step 上（升級 SKIPPED→PASSED）。
        self._current_idx = None
        if result.passed:
            self._publish("INFO", f"  ✅ Test PASSED: {data.name}")
        else:
            self._publish("ERROR", f"  ❌ Test FAILED: {data.name} — {result.message}")

    def start_keyword(self, data, result):
        # 檢查是不是 step marker
        kw_name = (data.name or "").strip()
        args = list(data.args or [])

        # 偵測 teardown 入口 → 立刻關掉 step 游標
        # （否則 teardown 內 PASS 的 keyword 會被誤算到最後一個 step）
        bare = kw_name.split(".")[-1].strip().lower()
        if bare in {"teardown browser session", "close browser"}:
            self._current_idx = None
            return

        if kw_name == "Log" and args and isinstance(args[0], str) and args[0].startswith(_STEP_MARKER_PREFIX):
            try:
                idx = int(args[0][len(_STEP_MARKER_PREFIX):].strip())
                self._current_idx = idx
                self._ensure_buf(idx)
                self._publish("INFO", f"    → Step {idx + 1} 開始")
            except ValueError:
                pass

    def end_keyword(self, data, result):
        kw_name = (data.name or "").strip()

        if self._current_idx is None:
            return

        buf = self._ensure_buf(self._current_idx)

        # Take Screenshot 結束 → 用 result.message 或回傳值取得檔名
        if "Take Screenshot" in kw_name:
            shot = None
            try:
                shot = result.message or ""
                # Browser library 通常 message 為檔案絕對路徑
                if shot and os.path.isfile(shot):
                    buf["_screenshots"].append(shot)
                else:
                    shot = None
            except Exception:
                pass
            return

        # ── 過濾掉非「真正 action」的 keyword ──────────────
        # marker
        if kw_name == "Log":
            return
        # instrumentation（截圖/紅框）以及外層的 Run Keyword And Ignore Error
        # （RKAIE 永遠 PASS，會誤升級 SKIPPED → PASSED）
        bare_name = kw_name.split(".")[-1].strip().lower()
        if bare_name in {
            "highlight elements",
            "take screenshot",
            "run keyword and ignore error",
            "run keyword and return status",
            "run keyword and continue on failure",
            "run keyword and expect error",
        }:
            return

        # 累計動作 keyword 的耗時與狀態
        try:
            dur = int(result.elapsed_time.total_seconds() * 1000)  # type: ignore[union-attr]
        except Exception:
            try:
                dur = int((result.endtime_seconds - result.starttime_seconds) * 1000)  # type: ignore[attr-defined]
            except Exception:
                dur = 0
        buf["_action_dur"] += dur

        status = (result.status or "").upper()
        if status == "FAIL":
            buf["status"] = "FAILED"
            buf["error"] = result.message or kw_name
        elif status == "PASS":
            # 只有 FAILED 不會被覆蓋；SKIPPED 升級為 PASSED
            if buf["status"] != "FAILED":
                buf["status"] = "PASSED"
        # status == "NOT RUN" / "SKIP" 等：保留現狀（預設 SKIPPED）

    def log_message(self, msg):
        # 把 INFO 以上層級的 log 推給前端
        try:
            level = (msg.level or "INFO").upper()
            txt = msg.message or ""
            if level in ("INFO", "WARN", "ERROR", "FAIL"):
                # 過濾 marker
                if txt.startswith(_STEP_MARKER_PREFIX):
                    return
                self._publish("ERROR" if level in ("ERROR", "FAIL") else level, txt[:500])
        except Exception:
            pass

    def message(self, msg):
        # Robot 系統訊息（Library import 等）暫不推送，避免噪音
        pass

    def close(self):
        # 把 buffer 整理成 list、解析 pre/post screenshot URL
        for idx in sorted(self._buffer.keys()):
            buf = self._buffer[idx]
            shots: list[str] = buf.pop("_screenshots", [])
            buf["duration_ms"] = buf.pop("_action_dur", 0)
            if shots:
                buf["pre"] = self._to_url(shots[0])
                if len(shots) >= 2:
                    buf["post"] = self._to_url(shots[-1])
            self._results.append(buf)

        # 寫入結果 JSON
        if self._result_path:
            try:
                os.makedirs(os.path.dirname(self._result_path), exist_ok=True)
                with open(self._result_path, "w", encoding="utf-8") as f:
                    json.dump(self._results, f, ensure_ascii=False)
            except Exception:
                pass

        if self._r:
            try:
                self._r.close()
            except Exception:
                pass

    # ── 內部 ──────────────────────────────────────────────
    def _to_url(self, abs_path: str) -> str | None:
        """把絕對路徑轉成對外可存取的 URL。

        STORAGE_BACKEND=minio 時會把檔案上傳到 ``results`` bucket 後
        回傳 ``/results/<key>``；否則退回原本的 ``/pics/<rel>`` 行為。
        """
        try:
            from app.config import settings  # 延遲 import 避免循環

            if (settings.STORAGE_BACKEND or "local").lower() == "minio":
                from app.services.storage_service import save_bytes

                with open(abs_path, "rb") as fh:
                    data = fh.read()
                key = f"screenshots/{os.path.basename(abs_path)}"
                return save_bytes(data, key, bucket="results", content_type="image/png")

            pic_root = os.path.abspath(settings.PIC_FOLDER)
            ap = os.path.abspath(abs_path)
            if ap.startswith(pic_root):
                rel = ap[len(pic_root):].replace("\\", "/").lstrip("/")
                return f"{settings.BASE_URL}/pics/{rel}"
        except Exception:
            pass
        return None
