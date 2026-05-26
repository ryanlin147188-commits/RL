"""
spawn 模式（execution_mode=docker）下，每個 testcase 啟動一個 ppodgorsek/robot-framework
衍生容器執行此 entrypoint。職責：
  1. 從環境變數讀取本次 case 的所有設定（task_id / report_id / case_tag / robot 內容
     的 MinIO key 等）
  2. 從 MinIO 下載 .robot 檔到本地 tmp
  3. 在 Xvfb 虛擬顯示下啟動 ``robot --listener tasks.robot_listener.RTListener ...``
  4. listener 在執行過程中會「即時上傳」每張截圖 / 影片切片 / trace.zip 到 MinIO
  5. 最後把 step_results.json 上傳到 MinIO，由外部 worker 讀取

不做：
  - 不寫資料庫（worker 端負責）
  - 不直接 publish WS 訊息（listener 內已透過 redis 推送到 task:{task_id}:logs）
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile

from app.services.storage_service import save_bytes  # type: ignore


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        if required:
            print(f"[robot_container] ERROR: env {name} required but missing", flush=True)
            sys.exit(2)
        return default or ""
    return val


def _download_from_minio(key: str, dest_path: str) -> None:
    """從 results bucket 拉檔到 dest_path。"""
    import boto3  # type: ignore

    endpoint = _env("S3_ENDPOINT", required=True)
    ak = _env("S3_ACCESS_KEY", required=True)
    sk = _env("S3_SECRET_KEY", required=True)
    bucket = _env("ROBOT_INPUT_BUCKET", "results")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        region_name="us-east-1",
    )
    s3.download_file(bucket, key, dest_path)


def _upload_local_file(local_path: str, key: str, content_type: str) -> str:
    if not os.path.isfile(local_path):
        return ""
    with open(local_path, "rb") as fh:
        data = fh.read()
    return save_bytes(data, key, bucket="results", content_type=content_type)


def main() -> int:
    task_id = _env("JOB_TASK_ID", required=True)
    report_id = _env("JOB_REPORT_ID", required=True)
    case_tag = _env("JOB_CASE_TAG", required=True)
    robot_key = _env("JOB_ROBOT_KEY", required=True)
    result_key = _env("JOB_RESULT_KEY", required=True)
    headless = _env("PLAYWRIGHT_HEADLESS", "1") not in ("0", "false", "False")

    # 我們在 .robot 裡用了 /work/videos 與 /work/traces 為 Browser Library 的 recordVideo dir，
    # 這裡確保兩個目錄存在；listener 會 glob 出檔案上傳到 MinIO。
    workdir = "/work"
    os.makedirs(os.path.join(workdir, "videos"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "traces"), exist_ok=True)
    robot_file = os.path.join(workdir, "test.robot")
    output_dir = os.path.join(workdir, "out")
    os.makedirs(output_dir, exist_ok=True)
    result_json = os.path.join(workdir, "step_results.json")

    # 1) 拉 .robot
    print(f"[robot_container] Downloading robot file: {robot_key}", flush=True)
    try:
        _download_from_minio(robot_key, robot_file)
    except Exception as e:  # noqa: BLE001
        print(f"[robot_container] ERROR downloading robot file: {e}", flush=True)
        return 3

    print(f"[robot_container] Saved robot to {robot_file} ({os.path.getsize(robot_file)} bytes)", flush=True)

    # 2) 環境變數給 listener
    env = os.environ.copy()
    env["AUTOTEST_TASK_ID"] = task_id
    env["AUTOTEST_REDIS_URL"] = _env("REDIS_URL", required=True)
    env["AUTOTEST_LOG_CHANNEL"] = f"task:{task_id}:logs"
    env["AUTOTEST_RESULT_PATH"] = result_json
    env["AUTOTEST_REPORT_ID"] = report_id
    env["AUTOTEST_CASE_TAG"] = case_tag
    env["AUTOTEST_OUTPUT_DIR"] = output_dir
    env["AUTOTEST_VIDEO_DIR"] = os.path.join(workdir, "videos")
    # listener 用此前綴決定上傳到 MinIO 後返回的 URL（與 backend BASE_URL 拼接）
    env["AUTOTEST_SCREENSHOT_URL_PREFIX"] = (
        f"{_env('BASE_URL', 'http://localhost')}/results/screenshots/{report_id}"
    )
    env["STORAGE_BACKEND"] = "s3"  # 強制走 SeaweedFS;spawn 模式下不接受其他 backend
    # ENABLE_RECORDING 由外部 worker 帶入（見 robot_runner.py）；listener 用來決定是否處理 video/trace
    if "ENABLE_RECORDING" not in env:
        env["ENABLE_RECORDING"] = "1"

    # 3) 跑 robot（headless 走 xvfb，避免 Browser Library 在無顯示器時 fallback 失敗）
    cmd_robot = [
        "robot",
        "--listener", "tasks.robot_listener.RTListener",
        "--outputdir", output_dir,
        "--loglevel", "INFO",
        robot_file,
    ]
    if headless:
        # 雖然我們在 .robot 內 `headless=true`，但 Browser Library 仍會初始化 X 連線；用 xvfb 包起來最穩
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24"] + cmd_robot
    else:
        cmd = cmd_robot

    print(f"[robot_container] Launching: {' '.join(cmd)}", flush=True)
    # robot subprocess 逾時:由 celery 端 ROBOT_SUBPROCESS_TIMEOUT_SEC 注入,
    # 跟 RUNNER_CONTAINER_TIMEOUT_SEC 對齊(預留 120s 給寫 JSON + 上傳 S3)。
    # 沒帶 env 就用 1680s(等於 1800s 預設容器 timeout - 120s 緩衝)。
    robot_timeout = int(os.environ.get("ROBOT_SUBPROCESS_TIMEOUT_SEC", "1680"))
    # 逾時時的 graceful shutdown 時限:先 SIGTERM,讓 RF Teardown 有時間跑完
    # (Close Browser → Playwright 寫完 .webm 的 WebM trailer 與 trace.zip 的
    # ending),確保影片/軌跡不會被截斷。30 秒仍不退才 SIGKILL。
    graceful_kill_sec = int(os.environ.get("ROBOT_GRACEFUL_KILL_SEC", "30"))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd="/app",
            env=env,
            preexec_fn=os.setsid,  # 新 process group,SIGTERM 一次發給整組
        )
        try:
            rc = proc.wait(timeout=robot_timeout)
        except subprocess.TimeoutExpired:
            print(
                f"[robot_container] WARN robot timed out ({robot_timeout}s) — "
                f"送 SIGTERM,等 {graceful_kill_sec}s 讓 Teardown 寫完 video/trace",
                flush=True,
            )
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception as e:  # noqa: BLE001
                print(f"[robot_container] ERROR SIGTERM 失敗: {e}", flush=True)
            try:
                rc = proc.wait(timeout=graceful_kill_sec)
                print(
                    f"[robot_container] robot 在 graceful 期內結束 rc={rc} "
                    f"(video / trace 應該完整)",
                    flush=True,
                )
            except subprocess.TimeoutExpired:
                print(
                    f"[robot_container] ERROR robot 拒絕在 {graceful_kill_sec}s 內 "
                    f"結束 → SIGKILL(video 可能不完整)",
                    flush=True,
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
                proc.wait()
                rc = 124
    except FileNotFoundError as e:
        print(f"[robot_container] ERROR cannot find robot binary: {e}", flush=True)
        rc = 127

    print(f"[robot_container] robot exited with rc={rc}", flush=True)

    # 4) 上傳 step_results.json
    if os.path.isfile(result_json):
        try:
            with open(result_json, "rb") as fh:
                save_bytes(fh.read(), result_key, bucket="results", content_type="application/json")
            print(f"[robot_container] Uploaded result JSON to {result_key}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[robot_container] ERROR uploading result JSON: {e}", flush=True)
    else:
        print(f"[robot_container] WARN result_json missing: {result_json}", flush=True)

    # 5) 清理 workdir
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
