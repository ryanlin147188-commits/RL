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
def run_tests(self, task_id: str, report_id: str, testcase_ids: list[str]):
    """
    執行指定的 testcase_ids：
      1. 從 testcase_contents 讀 steps_json + ddt_json
      2. 呼叫 Playwright runner 實際執行
      3. 將每步結果寫入 execution_steps_log
      4. 更新 execution_reports 統計與最終狀態
    """
    import redis

    r = redis.from_url(settings.REDIS_URL)
    channel = f"task:{task_id}:logs"

    def publish_log(level: str, msg: str) -> None:
        _pub(r, channel, f"[{_now()}] {msg}", level)

    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "1") not in ("0", "false", "False")

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

            try:
                round_results = run_testcase(
                    steps=steps,
                    ddt=ddt,
                    report_id=report_id,
                    case_tag=f"tc_{tc_id[:8]}",
                    publish_log=publish_log,
                    headless=headless,
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
                    for step_i, sr in enumerate(round_res.steps):
                        # SKIPPED 不寫入 DB（來自 Robot Framework 失敗後中止的順位步驟）
                        if sr.status == "SKIPPED":
                            continue
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
"""
Celery 執行任務：模擬 Playwright 腳本執行並即時發布 Log。

實際整合 Playwright 時，將 _simulate_step() 函式替換為：
    page.locator(selector).fill(value)
    bbox = await page.locator(selector).bounding_box()
    # 換算成百分比後存入 target_highlight_json

WebSocket Log 流向：
    Celery Worker → Redis pub/sub (channel: task:{task_id}:logs)
                  → FastAPI WS endpoint → 前端終端機
"""
import json
import random
import time
import uuid
from datetime import datetime

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
def run_tests(self, task_id: str, report_id: str, testcase_ids: list[str]):
    """
    執行指定的 testcase_ids，將結果寫入 DB 並透過 Redis 推送 Log。
    使用同步 SQLAlchemy (pymysql)，避免 Celery 與 asyncio 衝突。
    """
    import redis

    r = redis.from_url(settings.REDIS_URL)
    channel = f"task:{task_id}:logs"

    # 使用 sync driver (pymysql) 連接資料庫
    engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)

    try:
        _pub(r, channel, f"[{_now()}] 🚀 任務啟動 | Task: {task_id[:8]}...")
        _pub(r, channel, f"[{_now()}] 📋 共 {len(testcase_ids)} 個測試案例")

        with Session(engine) as db:
            from app.models.execution_report import ExecutionReport, ReportStatus
            from app.models.execution_step_log import ExecutionStepLog, StepStatus

            passed = 0
            failed = 0
            start_ts = time.time()

            for idx, tc_id in enumerate(testcase_ids, 1):
                _pub(r, channel, f"[{_now()}] ▶ [{idx}/{len(testcase_ids)}] 案例 {tc_id[:8]}...")
                case_start = time.time()
                case_passed = True

                # ── 每個 TESTCASE 模擬 3–6 個步驟 ────────────────────
                step_count = random.randint(3, 6)
                for step_i in range(step_count):
                    time.sleep(random.uniform(0.05, 0.3))  # 模擬執行耗時
                    step_failed = random.random() < 0.05   # 5% 失敗率
                    step_dur = int((time.time() - case_start) * 1000)

                    # 模擬 Playwright boundingBox（供前端畫紅框）
                    highlight = {
                        "top": f"{random.randint(20, 60)}%",
                        "left": f"{random.randint(10, 50)}%",
                        "width": f"{random.randint(20, 40)}%",
                        "height": f"{random.randint(5, 15)}%",
                    }

                    step_log = ExecutionStepLog(
                        id=str(uuid.uuid4()),
                        report_id=report_id,
                        testcase_node_id=tc_id,
                        step_index=step_i,
                        status=StepStatus.FAILED if step_failed else StepStatus.PASSED,
                        duration_ms=step_dur,
                        error_message="AssertionError: element #submit-btn not visible" if step_failed else None,
                        target_highlight_json=highlight,
                    )
                    db.add(step_log)

                    if step_failed:
                        case_passed = False
                        _pub(r, channel, f"[{_now()}]   ✗ Step {step_i + 1}: FAILED ({step_dur}ms)", "ERROR")
                        break
                    else:
                        _pub(r, channel, f"[{_now()}]   ✓ Step {step_i + 1}: PASSED ({step_dur}ms)")

                db.commit()

                if case_passed:
                    passed += 1
                    _pub(r, channel, f"[{_now()}] ✅ 案例 {idx} 通過")
                else:
                    failed += 1
                    _pub(r, channel, f"[{_now()}] ❌ 案例 {idx} 失敗", "ERROR")

            # ── 更新 execution_reports 最終狀態 ──────────────────────
            total_dur = int((time.time() - start_ts) * 1000)
            final_status = ReportStatus.PASSED if failed == 0 else ReportStatus.FAILED

            report: ExecutionReport | None = db.get(ExecutionReport, report_id)
            if report:
                report.status = final_status
                report.passed_cases = passed
                report.failed_cases = failed
                report.duration_ms = total_dur
                db.commit()

            _pub(
                r, channel,
                f"[{_now()}] 🏁 執行完成 ── 通過: {passed}  失敗: {failed}  耗時: {total_dur}ms"
            )
            _pub_done(r, channel, final_status.value)

    except Exception as exc:
        _pub(r, channel, f"[{_now()}] 💥 執行器異常: {exc}", "ERROR")
        _pub_done(r, channel, "FAILED")
        logger.exception("Task %s failed", task_id)
        raise
    finally:
        engine.dispose()
        r.close()
