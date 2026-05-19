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

from app.auth.context import current_org_id
from app.config import settings
from app.db.sync_session import SessionLocal
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
    setup_testcase_ids: list[str] | None = None,
):
    """執行指定的 testcase_ids:
      1. 從 testcase_contents 讀 steps_json + ddt_json
      2. 呼叫 Playwright runner 實際執行
      3. 將每步結果寫入 execution_steps_log
      4. 更新 execution_reports 統計與最終狀態

    setup_testcase_ids(v1.2):依排序先跑的「前置案例」。任一前置失敗 → 主案例
    全部標 SKIPPED-fail,整個任務狀態 FAILED。

    ddt_expand=False 時(預設):只用 DDT 第一列當變數上下文,整個 testcase 只跑一次。
    ddt_expand=True  時:依 DDT 每一列各自重跑一次 testcase。

    enable_recording=True 時(預設):
      - 啟用 Playwright Trace(trace.zip)與 Video(每案例一支 .webm + 每步驟切片)
      - 對應 URL 會寫到 ExecutionStepLog 的 trace_url / video_url(案例級,僅第一個 step)
        與 step_video_url(每個有 ffmpeg 切到的 step)
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

    try:
        from app.models.execution_report import ExecutionReport, ReportStatus
        from app.models.execution_step_log import ExecutionStepLog, StepStatus
        from app.models.testcase_content import TestcaseContent

        # Resolve the report's organisation upfront so every DB session this
        # task opens runs under the right tenant context. The ORM auto-stamp
        # listener (RFC-4) relies on ``current_org_id`` to fill ``organization_id``
        # on new rows like ExecutionStepLog inserts later in the loop.
        with SessionLocal() as db:
            _report = db.get(ExecutionReport, report_id)
            _org_id_token = current_org_id.set(
                _report.organization_id if _report else None
            )

        publish_log("INFO", f"🚀 任務啟動 | Task: {task_id[:8]}…")
        publish_log("INFO", f"📋 共 {len(testcase_ids)} 個測試案例")

        # 延遲 import：runner 載入會 import robot framework，未安裝時給出明確訊息
        try:
            from tasks.robot_runner import run_testcase
        except ImportError as e:
            publish_log("ERROR", f"💥 Robot Framework 未安裝：{e}")
            _pub_done(r, channel, "FAILED")
            with SessionLocal() as db:
                report = db.get(ExecutionReport, report_id)
                if report:
                    report.status = ReportStatus.FAILED
                    db.commit()
            return

        # ── 載入專案層級設定（環境變數 / 設備）一次，所有 testcase 共用 ──
        # 分開兩個 try 區塊，避免 ProjectDevice import 失敗時靜默吃掉環境變數載入
        project_env_vars: dict[str, str] = {}
        project_devices: list[dict] = []
        try:
            from app.models.project_env_var import ProjectEnvVar

            with SessionLocal() as db:
                report = db.get(ExecutionReport, report_id)
                if report and report.project_id:
                    pid = report.project_id
                    env_rows = db.query(ProjectEnvVar).filter(
                        ProjectEnvVar.project_id == pid
                    ).all()
                    project_env_vars = {row.name: row.value for row in env_rows}
            if project_env_vars:
                publish_log("INFO", f"🔑 載入專案環境變數 {len(project_env_vars)} 筆")
        except Exception as exc:  # noqa: BLE001
            publish_log("WARN", f"⚠ 載入專案環境變數失敗（將以空清單繼續）: {exc}")
        try:
            from app.models.project_device import ProjectDevice

            with SessionLocal() as db:
                report = db.get(ExecutionReport, report_id)
                if report and report.project_id:
                    pid = report.project_id
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
            if project_devices:
                publish_log("INFO", f"📱 載入專案設備 {len(project_devices)} 筆")
        except Exception:  # noqa: BLE001
            pass  # ProjectDevice 模組不存在時靜默略過，不影響環境變數載入

        passed_cases = 0
        failed_cases = 0
        start_ts = time.time()

        setup_ids_set = set(setup_testcase_ids or [])

        # ── Precondition continuity(v1.2.1): 把每個 main 的 setup chain 直接
        # 「inline」進 main 的 steps,一起送進同一個 runner 容器執行 — 才能讓
        # browser context / cookie / storage 在 setup 與 main 之間延續(不會
        # 因為換容器就遺失登入 state)。standalone 的 setup 迭代則跳過。
        from app.models.testcase_precondition_link import TestcasePreconditionLink
        from sqlalchemy import select as _select

        def _setup_chain_for(db, main_id: str, visited: set | None = None) -> list[str]:
            """BFS 展開 main 的 setup chain(含巢狀 precondition),deduped。"""
            visited = visited if visited is not None else set()
            chain: list[str] = []
            rows = db.execute(
                _select(TestcasePreconditionLink)
                .where(
                    TestcasePreconditionLink.testcase_id == main_id,
                    TestcasePreconditionLink.enabled.is_(True),
                )
                .order_by(
                    TestcasePreconditionLink.sort_order,
                    TestcasePreconditionLink.id,
                )
            ).scalars().all()
            for link in rows:
                pre = link.precondition_testcase_id
                if pre in visited:
                    continue
                visited.add(pre)
                chain.extend(_setup_chain_for(db, pre, visited))
                chain.append(pre)
            return chain

        main_to_setup_chain: dict[str, list[str]] = {}
        with SessionLocal() as db:
            for tc_id in testcase_ids:
                if tc_id in setup_ids_set:
                    continue  # 純 setup 不直接跑、它會被 inline 進相依的 main
                main_to_setup_chain[tc_id] = _setup_chain_for(db, tc_id)

        for idx, tc_id in enumerate(testcase_ids, 1):
            if tc_id in setup_ids_set:
                # 這個 setup 會在某個 main 的 inline chain 裡跑到,不單獨建立 runner
                continue

            chain = main_to_setup_chain.get(tc_id, [])
            phase_label = "▶"
            if chain:
                publish_log(
                    "INFO",
                    f"🔗 案例 {tc_id[:8]}… 前置 {len(chain)} 筆(在同一容器內串接執行)",
                )
            publish_log(
                "INFO",
                f"{phase_label} [{idx}/{len(testcase_ids)}] 案例 {tc_id[:8]}…",
            )

            # 載入 main 內容
            with SessionLocal() as db:
                content: Optional[TestcaseContent] = db.get(TestcaseContent, tc_id)

            if content is None or not content.steps_json:
                publish_log("ERROR", f"❌ 案例 {idx} 缺少 steps_json,跳過")
                failed_cases += 1
                continue

            main_steps = list(content.steps_json or [])
            ddt = content.ddt_json or {}

            # 依 chain 順序把 setup 的 steps 接在 main 前面;每條 step 標 _src_tc_id
            # 給 listener 之後可能用得到。標的同時不影響 robot_runner 的翻譯(它只
            # 讀 action / locator / input / 等已知欄位)。
            steps: list[dict] = []
            with SessionLocal() as db:
                for sid in chain:
                    sc = db.get(TestcaseContent, sid)
                    if not sc or not sc.steps_json:
                        publish_log("WARN", f"⚠ 前置案例 {sid[:8]} 缺 steps_json,跳過")
                        continue
                    for s in (sc.steps_json or []):
                        ss = dict(s)
                        ss["_src_tc_id"] = sid
                        ss["_phase"] = "setup"
                        steps.append(ss)
            for s in main_steps:
                ss = dict(s)
                ss["_src_tc_id"] = tc_id
                ss["_phase"] = "main"
                steps.append(ss)
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
                with SessionLocal() as db:
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

            # ── Per-testcase 歸屬 ─────────────────────────────────────
            # 因為 setup chain 跟 main 都 inline 進同一份 steps,RF 看到的是
            # 「合併後的 step index」(0..N-1)。但 DB 要按 source testcase 拆回去,
            # 否則 main 只 3 步、report 顯示「step 7」,前端找不到對應步驟內容。
            #
            # 構造 owner_map: combined_idx → (src_tc_id, local_idx),local_idx
            # 是 step 在「該 source testcase 內」的順序(從 0 開始)。
            owner_map: list[tuple[str, int]] = []
            _local_counter: dict[str, int] = {}
            for s in steps:
                src = s.get("_src_tc_id") or tc_id
                local = _local_counter.get(src, 0)
                owner_map.append((src, local))
                _local_counter[src] = local + 1

            # Per-testcase pass/fail:任一步 FAILED → 該 testcase 算 FAILED
            per_tc_failed: dict[str, bool] = {sid: False for sid in _local_counter.keys()}
            for rr in round_results:
                for i, sr in enumerate(rr.steps):
                    if sr.status != "FAILED":
                        continue
                    if i < len(owner_map):
                        per_tc_failed[owner_map[i][0]] = True
                    else:
                        per_tc_failed[tc_id] = True

            # 計入總計:chain 內每條 setup + main 各算一個 case
            for sid, fail in per_tc_failed.items():
                if fail:
                    failed_cases += 1
                else:
                    passed_cases += 1

            case_passed = not per_tc_failed.get(tc_id, False)
            if case_passed:
                publish_log("INFO", f"✅ 案例 {idx} 通過")
            else:
                publish_log("ERROR", f"❌ 案例 {idx} 失敗")
            if chain:
                fail_n = sum(1 for sid in chain if per_tc_failed.get(sid))
                pass_n = len(chain) - fail_n
                publish_log(
                    "INFO",
                    f"   前置 {len(chain)} 條:通過 {pass_n} / 失敗 {fail_n}",
                )

            # DDT 多列：用 step_index = round*1000 + local_idx 編碼,每個 source
            # testcase 內 local_idx 從 0 重新算。
            # 同時用 Python-side `created_at` 嚴格遞增(微秒級),確保 backend
            # ORDER BY (created_at, step_index) 可以正確還原「執行順序」:
            # setup steps 整段在前、main 在後,case 內按 step_index 排。
            # 若用 server_default NOW(),Postgres NOW() 是 transaction-scoped,
            # 整批 commit 拿到同一個 timestamp,排序就會等同 step_index 失效。
            from datetime import datetime, timedelta
            with SessionLocal() as db:
                _ts_base = datetime.utcnow()
                _ts_counter = 0
                for round_idx, round_res in enumerate(round_results):
                    # 找出本輪「main 第一個被寫入 DB 的 step」以掛 case 級 trace/video
                    main_first_persisted_idx: Optional[int] = None
                    for i, s in enumerate(round_res.steps):
                        if s.status == "SKIPPED":
                            continue
                        owner = owner_map[i][0] if i < len(owner_map) else tc_id
                        if owner == tc_id:
                            main_first_persisted_idx = i
                            break
                    for step_i, sr in enumerate(round_res.steps):
                        # SKIPPED 不寫入 DB（來自 Robot Framework 失敗後中止的順位步驟）
                        if sr.status == "SKIPPED":
                            continue
                        if step_i < len(owner_map):
                            owner_tc, local_idx = owner_map[step_i]
                        else:
                            owner_tc, local_idx = tc_id, step_i
                        is_main_anchor = (owner_tc == tc_id and step_i == main_first_persisted_idx)
                        db.add(
                            ExecutionStepLog(
                                id=str(uuid.uuid4()),
                                report_id=report_id,
                                testcase_node_id=owner_tc,
                                step_index=round_idx * 1000 + local_idx,
                                created_at=_ts_base + timedelta(microseconds=_ts_counter * 1000),
                                status=StepStatus(sr.status),
                                duration_ms=sr.duration_ms,
                                error_message=sr.error_message,
                                pre_screenshot_url=sr.pre_screenshot_url,
                                post_screenshot_url=sr.post_screenshot_url,
                                target_highlight_json=sr.target_highlight_json,
                                # case 級 trace / video 只掛在 main 案例的第一個 step 上
                                # (setup 步驟的 trace 包在同一份檔案內,從 main 入口看就行)
                                trace_url=round_res.trace_url if is_main_anchor else None,
                                video_url=round_res.video_url if is_main_anchor else None,
                                step_video_url=sr.step_video_url,
                                screenshot_baseline_url=sr.screenshot_baseline_url,
                                screenshot_diff_url=sr.screenshot_diff_url,
                                screenshot_diff_pct=sr.screenshot_diff_pct,
                            )
                        )
                        _ts_counter += 1
                db.commit()

        # ── 更新 execution_reports 最終狀態 ──────────────────────
        total_dur = int((time.time() - start_ts) * 1000)
        final_status = (
            ReportStatus.PASSED if failed_cases == 0 else ReportStatus.FAILED
        )

        with SessionLocal() as db:
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
        try:
            current_org_id.reset(_org_id_token)
        except (LookupError, NameError):
            # _org_id_token never bound — task aborted before ContextVar.set
            pass
        r.close()
