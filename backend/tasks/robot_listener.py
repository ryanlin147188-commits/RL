"""
Robot Framework Listener v3。

職責：
1. 即時把 keyword/test 事件 publish 到 Redis（給前端 WebSocket）。
2. **即時上傳**每張 Take Screenshot 的截圖到 MinIO，URL 寫入 step buffer。
3. 在 close()（suite 結束）時 glob 出 video / trace 檔，做 ffmpeg 切片並上傳，
   把 URL 注入對應 step buffer。
4. 把所有 step record 寫成 JSON：
   - 在 spawn 模式：寫到 ``AUTOTEST_RESULT_PATH``，由 robot_container.py 上傳到 MinIO
   - 在 in-process 模式：寫到本地路徑，由 robot_runner.py 直接讀

環境變數：
- AUTOTEST_REDIS_URL    : Redis 連線
- AUTOTEST_LOG_CHANNEL  : pub/sub channel（task:{task_id}:logs）
- AUTOTEST_RESULT_PATH  : 寫入 step 結果 JSON 的路徑
- AUTOTEST_REPORT_ID    : 用於決定 MinIO key 前綴（screenshots/<report_id>/...）
- AUTOTEST_VIDEO_DIR    : Browser Library recordVideo 的 dir（容器內路徑）
- AUTOTEST_OUTPUT_DIR   : Robot --outputdir，用來找 trace 檔
- ENABLE_RECORDING      : "1" 才做 video/trace 處理；"0" 則跳過
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import uuid
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
        self._report_id = os.environ.get("AUTOTEST_REPORT_ID", "unknown")
        self._video_dir = os.environ.get("AUTOTEST_VIDEO_DIR", "/work/videos")
        self._output_dir = os.environ.get("AUTOTEST_OUTPUT_DIR", "")
        self._enable_recording = os.environ.get("ENABLE_RECORDING", "1") not in ("0", "false", "False")

        try:
            self._r = redis.from_url(self._redis_url) if self._redis_url else None
        except Exception:  # noqa: BLE001
            self._r = None

        # 當前 step index（由 marker keyword 設定；None = 尚未進入任何 step）
        self._current_idx: int | None = None
        # 所有 step 結果 [{step_index, status, duration_ms, error, pre, post,
        #                  video_offset_start_ms, video_offset_end_ms, test_name}]
        self._results: list[dict[str, Any]] = []
        # 暫存每個 step 的累積資訊
        self._buffer: dict[int, dict[str, Any]] = {}
        # ── Trace / Video 計時 ─────────────────────────────────
        # 每個 test name 的錄影起始 wall time（time.time()），由 New Context 觸發
        self._test_recording_start: dict[str, float] = {}
        # 每個 test name 的「進入順序」（int）— 用來把 ${OUTPUT_DIR}/browser/traces_full/
        # 內按 mtime 排序的 trace.zip 與 test_name 對應
        self._test_order: dict[str, int] = {}
        self._current_test_name: str | None = None

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
        except Exception:  # noqa: BLE001
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
                # 即時上傳後的 MinIO URL list（pre = 第一張，post = 最後一張）
                "_screenshot_urls": [],
                "_action_dur": 0,
            }
        return self._buffer[idx]

    # ── Robot listener hooks ────────────────────────────
    def start_suite(self, data, result):
        self._publish("INFO", f"📁 Suite 開始: {data.name}")

    def start_test(self, data, result):
        # 進入新 test：重置 step 游標，避免上一個 test 的尾段事件污染
        self._current_idx = None
        self._current_test_name = data.name
        if data.name not in self._test_order:
            self._test_order[data.name] = len(self._test_order)
        self._publish("INFO", f"  ▶ Test: {data.name}")

    def end_test(self, data, result):
        # test 結束後 [Teardown] 的 keyword 仍會 fire；要立刻關掉游標，
        # 否則 teardown 內的 PASS 會被誤算到最後一個 step 上（升級 SKIPPED→PASSED）。
        # 同時把最後一個 step 的錄影結束時間補上（最後一步沒有後續 AT_STEP marker 來觸發）。
        if self._current_idx is not None:
            buf = self._ensure_buf(self._current_idx)
            buf.setdefault("_end_wall", time.time())
        self._current_idx = None
        if result.passed:
            self._publish("INFO", f"  ✅ Test PASSED: {data.name}")
        else:
            self._publish("ERROR", f"  ❌ Test FAILED: {data.name} — {result.message}")

    def start_keyword(self, data, result):
        # 檢查是不是 step marker
        kw_name = (data.name or "").strip()
        args = list(data.args or [])
        now = time.time()

        # 偵測 teardown 入口 → 立刻關掉 step 游標
        # （否則 teardown 內 PASS 的 keyword 會被誤算到最後一個 step）
        bare = kw_name.split(".")[-1].strip().lower()
        if bare in {"teardown browser session", "close browser"}:
            if self._current_idx is not None:
                buf = self._ensure_buf(self._current_idx)
                buf.setdefault("_end_wall", now)
            self._current_idx = None
            return

        # 偵測 New Context（Browser Library）→ 視為錄影起始時間
        # （recordVideo 在 context 建立時開始；此時 wall time 即影片 0:00）
        if bare == "new context" and self._current_test_name:
            self._test_recording_start.setdefault(self._current_test_name, now)
            return

        if kw_name == "Log" and args and isinstance(args[0], str) and args[0].startswith(_STEP_MARKER_PREFIX):
            try:
                idx = int(args[0][len(_STEP_MARKER_PREFIX):].strip())
            except ValueError:
                return
            # 切換 step 時：替「上一個 step」補 _end_wall（end = 下一個 marker 出現的時刻）
            if self._current_idx is not None and self._current_idx != idx:
                prev = self._ensure_buf(self._current_idx)
                prev.setdefault("_end_wall", now)
            self._current_idx = idx
            buf = self._ensure_buf(idx)
            buf["_start_wall"] = now
            if self._current_test_name:
                buf["_test_name"] = self._current_test_name
            self._publish("INFO", f"    → Step {idx + 1} 開始")

    def end_keyword(self, data, result):
        kw_name = (data.name or "").strip()

        if self._current_idx is None:
            return

        buf = self._ensure_buf(self._current_idx)

        # Take Screenshot 結束 → 即時上傳到 MinIO，並把 URL 收集到 buf["_screenshot_urls"]
        # Browser Library 19.x：`result.message` 不再是純檔名，可能是 "Screenshot saved to:
        # /path/to/file.png" 之類的字串。為了穩健，採三段嘗試：
        #   a) result.message 本身是檔案路徑 → 直接用
        #   b) 從 result.message 內 regex 抓 .png/.jpeg/.webp 路徑
        #   c) data.args 內找 filename=... 然後加副檔名探查存在的檔
        if "Take Screenshot" in kw_name:
            try:
                shot_path = self._resolve_screenshot_path(result, data)
                if shot_path:
                    url = self._upload_screenshot(shot_path)
                    if url:
                        buf.setdefault("_screenshot_urls", []).append(url)
                else:
                    self._publish("WARN", f"截圖路徑解析失敗: msg={getattr(result, 'message', '')!r}")
            except Exception as e:  # noqa: BLE001
                self._publish("WARN", f"截圖上傳失敗: {e}")
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
        except Exception:  # noqa: BLE001
            try:
                dur = int((result.endtime_seconds - result.starttime_seconds) * 1000)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                dur = 0
        buf["_action_dur"] += dur

        status = (result.status or "").upper()
        if status == "FAIL":
            buf["status"] = "FAILED"
            # 第一個 FAIL 的訊息才是真正 root cause;後續 keyword 通常是 cascade
            # (例如 Get Text 失敗 → ${actual} 沒被設 → 下一條 Should... 報
            # "Variable not found")。保留第一個錯誤、不覆寫,讓 report 顯示真因。
            if not buf.get("error"):
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
                # SCREENSHOT_DIFF marker：assert_screenshot_lib 寫的、解析後寫進 step buf
                if txt.startswith("SCREENSHOT_DIFF "):
                    self._handle_screenshot_diff_marker(txt)
                    return
                self._publish("ERROR" if level in ("ERROR", "FAIL") else level, txt[:500])
        except Exception:  # noqa: BLE001
            pass

    def _handle_screenshot_diff_marker(self, txt: str) -> None:
        """解析 ``SCREENSHOT_DIFF step_uuid=... baseline=... actual=... diff=... pct=...``
        並把 URL 與 diff% 寫入「目前 step」的 buffer。
        """
        if self._current_idx is None:
            return
        # 簡單 token=value 解析（value 不含空白）
        parts = txt.split()
        kv: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                k, _, v = p.partition("=")
                kv[k] = v
        buf = self._ensure_buf(self._current_idx)
        if "baseline" in kv and kv["baseline"]:
            buf["screenshot_baseline_url"] = kv["baseline"]
        if "diff" in kv and kv["diff"]:
            buf["screenshot_diff_url"] = kv["diff"]
        if "actual" in kv and kv["actual"]:
            # 也存一份「actual」URL 進 post（讓 report 顯示當下截圖）；
            # 若該 step 之後還有 Take Screenshot 會覆蓋掉，這裡是 best-effort
            buf.setdefault("_screenshot_urls", []).append(kv["actual"])
        if "pct" in kv:
            try:
                buf["screenshot_diff_pct"] = float(kv["pct"])
            except ValueError:
                pass

    def message(self, msg):
        # Robot 系統訊息（Library import 等）暫不推送，避免噪音
        pass

    def close(self):
        # 1) 把 buffer 內截圖 URL / video offset 整理出來（截圖已在 end_keyword 即時上傳）
        for idx in sorted(self._buffer.keys()):
            buf = self._buffer[idx]
            urls: list[str] = buf.pop("_screenshot_urls", [])
            buf["duration_ms"] = buf.pop("_action_dur", 0)
            if urls:
                buf["pre"] = urls[0]
                if len(urls) >= 2:
                    buf["post"] = urls[-1]

            # 錄影 offset：相對於該 test 的 New Context wall time（影片 0:00）
            test_name = buf.pop("_test_name", None)
            sw = buf.pop("_start_wall", None)
            ew = buf.pop("_end_wall", None)
            rec_start = self._test_recording_start.get(test_name) if test_name else None
            if rec_start is not None and isinstance(sw, (int, float)):
                buf["video_offset_start_ms"] = max(0, int((sw - rec_start) * 1000))
            if rec_start is not None and isinstance(ew, (int, float)):
                buf["video_offset_end_ms"] = max(0, int((ew - rec_start) * 1000))
            if test_name:
                buf["test_name"] = test_name

            self._results.append(buf)

        # 2) 處理 video / trace（spawn + STORAGE_BACKEND=s3 時才有意義）
        if self._enable_recording and self._is_s3_mode():
            try:
                self._process_videos_and_traces()
            except Exception as e:  # noqa: BLE001
                self._publish("WARN", f"video/trace 處理失敗: {e}")

        # 3) 寫入結果 JSON（spawn 模式：寫到本地檔，由 robot_container.py 上傳到 SeaweedFS）
        if self._result_path:
            try:
                os.makedirs(os.path.dirname(self._result_path), exist_ok=True)
                with open(self._result_path, "w", encoding="utf-8") as f:
                    json.dump(self._results, f, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001
                self._publish("ERROR", f"寫入 step_results.json 失敗: {e}")

        if self._r:
            try:
                self._r.close()
            except Exception:  # noqa: BLE001
                pass

    # ── video/trace 處理 ─────────────────────────────────
    def _is_s3_mode(self) -> bool:
        try:
            from app.config import settings  # type: ignore
            return (settings.STORAGE_BACKEND or "").lower() == "s3"
        except Exception:  # noqa: BLE001
            return False

    def _process_videos_and_traces(self) -> None:
        """
        spawn 容器收尾：
          - 找出每個 test 的完整 .webm 與 trace.zip
          - 上傳到 MinIO，URL 寫入該 test 第一個被持久化 step 的 video_url / trace_url

        不再做 ffmpeg 步驟切片（user 要求拿掉）。
        """
        test_names_sorted = sorted(self._test_order.keys(), key=self._test_order.get)
        if not test_names_sorted:
            return

        video_files = self._list_videos()
        trace_files = self._list_traces()
        self._publish("INFO", f"[listener] found {len(video_files)} videos, {len(trace_files)} traces")

        for i, test_name in enumerate(test_names_sorted):
            video_path = video_files[i] if i < len(video_files) else None
            trace_path = trace_files[i] if i < len(trace_files) else None

            video_url = self._upload_video(video_path, test_name) if video_path else None
            trace_url = self._upload_trace(trace_path, test_name) if trace_path else None

            test_steps = [r for r in self._results if r.get("test_name") == test_name]
            test_steps.sort(key=lambda r: r.get("step_index", 0))
            anchor = next((r for r in test_steps if r.get("status") != "SKIPPED"), None)
            if anchor:
                if video_url:
                    anchor["video_url"] = video_url
                if trace_url:
                    anchor["trace_url"] = trace_url

    def _list_videos(self) -> list[str]:
        if not os.path.isdir(self._video_dir):
            return []
        files = [
            os.path.join(self._video_dir, fn)
            for fn in os.listdir(self._video_dir)
            if fn.lower().endswith(".webm")
        ]
        files.sort(key=lambda p: os.path.getmtime(p))
        return files

    def _list_traces(self) -> list[str]:
        """
        Browser Library 19.x 把 trace 寫到 ``${OUTPUT_DIR}`` 之下，實際路徑視 tracing 參數而定：
          - tracing=True 時通常落在 ``${OUTPUT_DIR}/browser/traces/<test_name>.zip`` 或 traces_full
          - tracing=Path("xxx.zip") 時落在 ``${OUTPUT_DIR}/xxx.zip``
        為了盡量別漏抓，全 outputdir 遞迴 glob 所有 .zip 都收進來。
        """
        if not self._output_dir or not os.path.isdir(self._output_dir):
            return []
        candidates = glob.glob(os.path.join(self._output_dir, "**", "*.zip"), recursive=True)
        candidates.sort(key=lambda p: os.path.getmtime(p))
        return candidates

    def _resolve_screenshot_path(self, result, data) -> str | None:
        """嘗試多種方式找出 Take Screenshot 寫出的檔案路徑。"""
        # a) result.message 本身是檔名
        msg = (getattr(result, "message", "") or "").strip()
        if msg and os.path.isfile(msg):
            return msg

        # b) message 內含路徑（19.x 會 log "Screenshot is taken to: /path/to/file.png" 之類）
        m = re.search(r"(/\S+\.(?:png|jpe?g|webp))", msg, re.IGNORECASE)
        if m and os.path.isfile(m.group(1)):
            return m.group(1)

        # c) data.args 找 filename= 並補副檔名探查
        try:
            for arg in (data.args or []):
                if isinstance(arg, str) and arg.lower().startswith("filename="):
                    base = arg.split("=", 1)[1]
                    for ext in (".png", ".jpeg", ".jpg", ".webp"):
                        candidate = base + ext
                        if os.path.isfile(candidate):
                            return candidate
                    # 退而求其次：base 名稱前綴匹配（Browser Library 可能加 _1 / timestamp）
                    parent = os.path.dirname(base) or "."
                    bn = os.path.basename(base)
                    if os.path.isdir(parent):
                        matches = sorted(
                            os.path.join(parent, fn) for fn in os.listdir(parent)
                            if fn.startswith(bn) and fn.lower().endswith((".png", ".jpeg", ".jpg", ".webp"))
                        )
                        if matches:
                            return matches[-1]
        except Exception:  # noqa: BLE001
            pass
        return None

    def _upload_screenshot(self, abs_path: str) -> str | None:
        """即時把單張 .png 上傳到 MinIO；回傳 ``/results/...`` URL（或 None）。"""
        try:
            from app.services.storage_service import save_bytes  # type: ignore

            with open(abs_path, "rb") as fh:
                data = fh.read()
            key = f"screenshots/{self._report_id}/{uuid.uuid4().hex}_{os.path.basename(abs_path)}"
            return save_bytes(data, key, bucket="results", content_type="image/png")
        except Exception as e:  # noqa: BLE001
            self._publish("WARN", f"_upload_screenshot 失敗: {e}")
            return None

    def _upload_video(self, abs_path: str, test_name: str) -> str | None:
        try:
            import subprocess, tempfile, os
            from app.services.storage_service import save_bytes  # type: ignore

            # 嘗試用 ffmpeg 轉 mp4（celery 容器內有 ffmpeg）
            mp4_path = abs_path.replace(".webm", ".mp4")
            converted = False
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", abs_path, "-c:v", "copy", "-c:a", "copy", mp4_path],
                    capture_output=True, timeout=120, check=True
                )
                converted = True
            except Exception:  # noqa: BLE001
                pass

            if converted and os.path.exists(mp4_path):
                with open(mp4_path, "rb") as fh:
                    data = fh.read()
                key = f"videos/{self._report_id}/{test_name}.mp4"
                try:
                    os.remove(mp4_path)
                except Exception:  # noqa: BLE001
                    pass
                return save_bytes(data, key, bucket="results", content_type="video/mp4")

            # fallback：直接上傳 webm
            with open(abs_path, "rb") as fh:
                data = fh.read()
            key = f"videos/{self._report_id}/{test_name}.webm"
            return save_bytes(data, key, bucket="results", content_type="video/webm")
        except Exception as e:  # noqa: BLE001
            self._publish("WARN", f"_upload_video 失敗: {e}")
            return None

    def _upload_trace(self, abs_path: str, test_name: str) -> str | None:
        try:
            from app.services.storage_service import save_bytes  # type: ignore

            with open(abs_path, "rb") as fh:
                data = fh.read()
            key = f"traces/{self._report_id}/{test_name}.zip"
            return save_bytes(data, key, bucket="results", content_type="application/zip")
        except Exception as e:  # noqa: BLE001
            self._publish("WARN", f"_upload_trace 失敗: {e}")
            return None

    # ── 內部 ──────────────────────────────────────────────
    def _to_url(self, abs_path: str) -> str | None:
        """把絕對路徑轉成對外可存取的 URL。

        把檔案上傳到 ``results`` bucket(SeaweedFS),回傳 ``/results/<key>``。
        """
        try:
            from app.services.storage_service import save_bytes

            with open(abs_path, "rb") as fh:
                data = fh.read()
            key = f"screenshots/{os.path.basename(abs_path)}"
            return save_bytes(data, key, bucket="results", content_type="image/png")
        except Exception:  # noqa: BLE001
            pass
        return None
