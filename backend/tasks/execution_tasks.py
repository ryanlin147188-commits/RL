"""
Celery 執行任務：呼叫 Playwright runner 真實執行測試並即時發布 Log。

WebSocket Log 流向：
    Celery Worker → Redis pub/sub (channel: task:{task_id}:logs)
                  → FastAPI WS endpoint → 前端終端機

若 testcase 內缺 steps_json 則該案例直接記為 FAILED。
"""
import json
import os
import time
import uuid
from datetime import datetime
from typing import Optional

from celery.utils.log import get_task_logger
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from tasks.celery_app import celery_app

logger = get_task_logger(__name__)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _pub(r, channel: str, msg: str, level: str = "INFO") -> None:
    r.publish(channel, json.dumps({"type": "log", "level": level, "message": msg}))


def _pub_done(r, channel: str, status: str) -> None:
    r.publish(channel, json.dumps({"type": "done", "status": status}))


@celery_app.task(bind=True, name="tasks.execution_tasks.run_tests")
def run_tests(
    self,
    task_id: str,
    report_id: str,
    testcase_ids: list[str],
    ddt_expand: bool = False,
    enable_recording: bool = True,
):
    """
    執行指定的 testcase_ids：
      1. 從 testcase_contents 讀 steps_json + ddt_json
      2. 呼叫 Playwright runner 實際執行
      3. 將每步結果寫入 execution_steps_log
      4. 更新 execution_reports 統計與最終狀態

    ddt_expand=False 時（預設）：只用 DDT 第一列當變數上下文，整個 testcase 只跑一次。
    ddt_expand=True  時：依 DDT 每一列各自重跑一次 testcase。

    enable_recording=True 時（預設）：
      - 啟用 Playwright Trace（trace.zip）與 Video（每案例一支 .webm + 每步驟切片）
      - 對應 URL 會寫到 ExecutionStepLog 的 trace_url / video_url（案例級，僅第一個 step）
        與 step_video_url（每個有 ffmpeg 切到的 step）
    """
    import redis

    r = redis.from_url(settings.REDIS_URL)
    channel = f"task:{task_id}:logs"

    def publish_log(level: str, msg: str) -> None:
        _pub(r, channel, f"[{_now()}] {msg}", level)

    # 預設改為有頭模式（headed），讓瀏覽器視覺化執行方便除錯。
    # Celery container 已安裝 xvfb，實際顯示走虛擬 framebuffer。
    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "0") not in ("0", "false", "False")

    # 供 robot_runner 子進程的 listener 讀取，以使用同一條 Redis channel
    os.environ["AUTOTEST_TASK_ID"] = task_id

    engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)

    try:
        from app.models.execution_report import ExecutionReport, ReportStatus
        from app.models.execution_step_log import ExecutionStepLog, StepStatus
        from app.models.testcase_content import TestcaseContent

        publish_log("INFO", f"🚀 任務啟動 | Task: {task_id[:8]}…")
        publish_log("INFO", f"📋 共 {len(testcase_ids)} 個測試案例")

        # 延遲 import：runner 載入會 import robot framework，未安裝時給出明確訊息
        try:
            from tasks.robot_runner import run_testcase
        except ImportError as e:
            publish_log("ERROR", f"💥 Robot Framework 未安裝：{e}")
            _pub_done(r, channel, "FAILED")
            with Session(engine) as db:
                report = db.get(ExecutionReport, report_id)
                if report:
                    report.status = ReportStatus.FAILED
                    db.commit()
            return

        # ── 載入專案層級設定（環境變數 / 設備）一次，所有 testcase 共用 ──
        # 撈 report.project_id，再依 project_id 撈 project_env_vars + project_devices
        project_env_vars: dict[str, str] = {}
        project_devices: list[dict] = []
        try:
            from app.models.project_device import ProjectDevice
            from app.models.project_env_var import ProjectEnvVar

            with Session(engine) as db:
                report = db.get(ExecutionReport, report_id)
                if report and report.project_id:
                    pid = report.project_id
                    env_rows = db.query(ProjectEnvVar).filter(
                        ProjectEnvVar.project_id == pid
                    ).all()
                    project_env_vars = {row.name: row.value for row in env_rows}

                    dev_rows = db.query(ProjectDevice).filter(
                        ProjectDevice.project_id == pid
                    ).all()
                    project_devices = [
                        {
                            "label": d.label,
                            "platform": d.platform.value if hasattr(d.platform, "value") else str(d.platform),
                            "platform_version": d.platform_version,
                            "device_name": d.device_name,
                            "avd_name": d.avd_name,
                            "udid": d.udid,
                            "automation_name": d.automation_name,
                            "extra_caps_json": d.extra_caps_json or {},
                        }
                        for d in dev_rows
                    ]
            if project_env_vars:
                publish_log("INFO", f"🔑 載入專案環境變數 {len(project_env_vars)} 筆")
            if project_devices:
                publish_log("INFO", f"📱 載入專案設備 {len(project_devices)} 筆")
        except Exception as exc:  # noqa: BLE001
            publish_log("WARN", f"⚠ 載入專案設定失敗（將以空清單繼續）: {exc}")

        passed_cases = 0
        failed_cases = 0
        start_ts = time.time()

        for idx, tc_id in enumerate(testcase_ids, 1):
            publish_log("INFO", f"▶ [{idx}/{len(testcase_ids)}] 案例 {tc_id[:8]}…")

            with Session(engine) as db:
                content: Optional[TestcaseContent] = db.get(TestcaseContent, tc_id)

            if content is None or not content.steps_json:
                publish_log("ERROR", f"❌ 案例 {idx} 缺少 steps_json，跳過")
                failed_cases += 1
                continue

            steps = content.steps_json or []
            ddt = content.ddt_json or {}
            # ddt_expand=False → 最多只跑一輪（以 DDT 第一列當變數）
            if not ddt_expand and ddt and isinstance(ddt, dict):
                rows = ddt.get("rows") or []
                if len(rows) > 1:
                    ddt = {
                        "headers": ddt.get("headers") or [],
                        "rows": rows[:1],
                    }

            try:
                round_results = run_testcase(
                    steps=steps,
                    ddt=ddt,
                    report_id=report_id,
                    case_tag=f"tc_{tc_id[:8]}",
                    publish_log=publish_log,
                    headless=headless,
                    enable_recording=enable_recording,
                    project_env_vars=project_env_vars,
                    project_devices=project_devices,
                )
            except Exception as exc:  # noqa: BLE001
                publish_log("ERROR", f"💥 案例 {idx} 執行器異常: {exc}")
                failed_cases += 1
                with Session(engine) as db:
                    db.add(
                        ExecutionStepLog(
                            id=str(uuid.uuid4()),
                            report_id=report_id,
                            testcase_node_id=tc_id,
                            step_index=0,
                            status=StepStatus.FAILED,
                            duration_ms=0,
                            error_message=f"Runner exception: {exc}",
                        )
                    )
                    db.commit()
                continue

            case_passed = all(rr.passed for rr in round_results)
            if case_passed:
                passed_cases += 1
                publish_log("INFO", f"✅ 案例 {idx} 通過")
            else:
                failed_cases += 1
                publish_log("ERROR", f"❌ 案例 {idx} 失敗")

            # DDT 多列：用 step_index = round*1000 + step 編碼
            with Session(engine) as db:
                for round_idx, round_res in enumerate(round_results):
                    # 找出本輪「第一個會被寫入 DB 的 step」(非 SKIPPED) 以掛 case 級欄位
                    first_persisted_step: Optional[int] = next(
                        (i for i, s in enumerate(round_res.steps) if s.status != "SKIPPED"),
                        None,
                    )
                    for step_i, sr in enumerate(round_res.steps):
                        # SKIPPED 不寫入 DB（來自 Robot Framework 失敗後中止的順位步驟）
                        if sr.status == "SKIPPED":
                            continue
                        is_case_anchor = step_i == first_persisted_step
                        db.add(
                            ExecutionStepLog(
                                id=str(uuid.uuid4()),
                                report_id=report_id,
                                testcase_node_id=tc_id,
                                step_index=round_idx * 1000 + step_i,
                                status=StepStatus(sr.status),
                                duration_ms=sr.duration_ms,
                                error_message=sr.error_message,
                                pre_screenshot_url=sr.pre_screenshot_url,
                                post_screenshot_url=sr.post_screenshot_url,
                                target_highlight_json=sr.target_highlight_json,
                                # case 級 trace / video 只掛在本輪第一個被持久化的 step 上
                                trace_url=round_res.trace_url if is_case_anchor else None,
                                video_url=round_res.video_url if is_case_anchor else None,
                                step_video_url=sr.step_video_url,
                                screenshot_baseline_url=sr.screenshot_baseline_url,
                                screenshot_diff_url=sr.screenshot_diff_url,
                                screenshot_diff_pct=sr.screenshot_diff_pct,
                            )
                        )
                db.commit()

        # ── 更新 execution_reports 最終狀態 ──────────────────────
        total_dur = int((time.time() - start_ts) * 1000)
        final_status = (
            ReportStatus.PASSED if failed_cases == 0 else ReportStatus.FAILED
        )

        with Session(engine) as db:
            report = db.get(ExecutionReport, report_id)
            if report:
                report.status = final_status
                report.passed_cases = passed_cases
                report.failed_cases = failed_cases
                report.duration_ms = total_dur
                db.commit()

        publish_log(
            "INFO",
            f"🏁 執行完成 ── 通過: {passed_cases}  失敗: {failed_cases}  耗時: {total_dur}ms",
        )
        _pub_done(r, channel, final_status.value)

    except Exception as exc:
        publish_log("ERROR", f"💥 執行器異常: {exc}")
        _pub_done(r, channel, "FAILED")
        logger.exception("Task %s failed", task_id)
        raise
    finally:
        engine.dispose()
        r.close()
