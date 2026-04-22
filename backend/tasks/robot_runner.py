"""
Robot Framework 執行引擎。

被 tasks.execution_tasks.run_tests 呼叫。
- 將 steps_json + ddt_json 動態組成 .robot 檔
- subprocess 呼叫 `robot` CLI；附 --listener tasks.robot_listener.RTListener
- listener 即時 publish 到 Redis（給前端 WS）並把每步結果寫入 JSON
- 解析 JSON → 回傳 CaseResult 給 caller 寫入 ExecutionStepLog

steps_json 中 action 規則：
- 預設使用 Browser Library（Playwright）
- 前綴 `Http.` → RequestsLibrary
- 前綴 `Db.`   → DatabaseLibrary
- 前綴 `Mobile.` → AppiumLibrary

DDT：每一列產生一個 Test Case，header 變成 ${var} 變數可在 locator/input/expected 中參照。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import settings


@dataclass
class StepResult:
    status: str                  # "PASSED" | "FAILED" | "SKIPPED"
    duration_ms: int
    error_message: Optional[str]
    pre_screenshot_url: Optional[str]
    post_screenshot_url: Optional[str]
    target_highlight_json: Optional[dict]  # 在 RF 模式下保留欄位但通常為 None


@dataclass
class CaseResult:
    passed: bool
    steps: list[StepResult]
    duration_ms: int


# ════════════════════════════════════════════════════════════════
# 變數替換 + Robot 字串跳脫
# ════════════════════════════════════════════════════════════════

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _substitute(text: Any, ctx: dict) -> str:
    """將 ${var} / $var 用 ctx 取代；非字串轉成空字串。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    def repl(m: re.Match) -> str:
        key = m.group(1) or m.group(2)
        if key in ctx:
            return str(ctx[key])
        if f"${key}" in ctx:
            return str(ctx[f"${key}"])
        return m.group(0)

    return _VAR_PATTERN.sub(repl, text)


def _rf_escape(value: str) -> str:
    """跳脫 Robot 字串中可能造成解析問題的字元。"""
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace("\t", " ").replace("\r", "")
    # 換行替成空白避免破壞 RF 表格
    s = s.replace("\n", " ")
    # 開頭若為 # 會被當註解，加跳脫
    if s.startswith("#"):
        s = "\\" + s
    return s


def _looks_named(s: str) -> bool:
    """檢查字串是否會被 RF 解析為 named argument（如 text=foo / css=#x）。

    RF 在 named-arg 偵測階段會把任何 `name=value` 形式視為 keyword 命名參數，
    且 .robot 來源的 `\\=` 跳脫會在此階段之前還原 → 跳脫無效。
    解決方式是把這類值放進變數再傳遞。
    """
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_-]*=", s or ""))


# ════════════════════════════════════════════════════════════════
# action → Robot keyword 轉譯
# ════════════════════════════════════════════════════════════════


def _translate_step(step: dict, ctx: dict) -> list[str]:
    """
    將單一 step 翻譯成 Robot 表格行 list（每行已 4-space 縮排）。
    回傳的行**不包含** marker 與截圖；那些由 caller 統一加上。
    """
    raw_action = (step.get("action") or "").strip()
    action = raw_action.lower()
    # 兼容兩種欄位命名：locator (舊) / loc (前端與錄製器產生)
    raw_locator = step.get("locator") or step.get("loc") or ""
    locator = _rf_escape(_substitute(raw_locator, ctx))
    value = _rf_escape(_substitute(step.get("input") or "", ctx))
    expected = _rf_escape(_substitute(step.get("expected") or "", ctx))

    def line(*parts: str) -> str:
        return "    " + "    ".join(p for p in parts)

    # ── 把 named-form 的 locator / value 預先存入變數，
    #    避免 RF 把 `text=...` / `css=...` 誤判為命名參數 ────
    prelude: list[str] = []
    if _looks_named(locator):
        prelude.append(line("${LOC}=", "Set Variable", locator))
        locator = "${LOC}"
    if _looks_named(value):
        prelude.append(line("${VAL}=", "Set Variable", value))
        value = "${VAL}"
    if _looks_named(expected):
        prelude.append(line("${EXP}=", "Set Variable", expected))
        expected = "${EXP}"

    def out(*body: list[str] | str) -> list[str]:
        result: list[str] = list(prelude)
        for b in body:
            if isinstance(b, list):
                result.extend(b)
            else:
                result.append(b)
        return result

    # ── Browser Library（預設）────────────────────────
    if action in ("goto", "navigate", "open"):
        target = value or expected or locator
        return out(line("Go To", target))
    if action == "click":
        return out(line("Click", locator))
    if action in ("doubleclick", "dblclick"):
        return out(line("Click", locator, "clickCount=2"))
    if action == "rightclick":
        return out(line("Click", locator, "button=right"))
    if action in ("fill", "input"):
        return out(line("Fill Text", locator, value))
    if action == "type":
        return out(line("Type Text", locator, value))
    if action == "press":
        # Browser Library 用 Keyboard Key 或 Press Keys
        return out(line("Press Keys", locator, value or "Enter"))
    if action == "hover":
        return out(line("Hover", locator))
    if action == "check":
        return out(line("Check Checkbox", locator))
    if action == "uncheck":
        return out(line("Uncheck Checkbox", locator))
    if action == "select":
        return out(line("Select Options By", locator, "value", value))
    if action in ("wait", "sleep"):
        ms = value or expected or "1000"
        # Browser 用 Sleep（BuiltIn）；單位 ms
        return out(line("Sleep", f"{int(float(ms)) / 1000.0}s"))
    if action in ("waitforselector", "waitfor"):
        return out(line("Wait For Elements State", locator, "visible"))
    if action in ("assertvisible", "shouldbevisible"):
        return out(line("Wait For Elements State", locator, "visible"))
    if action in ("asserthidden", "shouldbehidden"):
        return out(line("Wait For Elements State", locator, "hidden"))
    if action == "asserttext":
        return out(
            line("${actual}=", "Get Text", locator),
            line("Should Contain", "${actual}", expected),
        )
    if action == "assertvalue":
        return out(
            line("${actual}=", "Get Property", locator, "value"),
            line("Should Be Equal As Strings", "${actual}", expected),
        )
    if action == "asserturl":
        return out(
            line("${url}=", "Get Url"),
            line("Should Contain", "${url}", expected),
        )

    # ── HTTP（RequestsLibrary）────────────────────────
    if raw_action.startswith("Http."):
        method = raw_action.split(".", 1)[1].upper()
        # locator = url；input = body(json) 或空；expected = 預期 status code
        if method in ("GET", "DELETE"):
            kw = "GET" if method == "GET" else "DELETE"
            return out(
                line(f"${{resp}}=", kw, locator, "expected_status=any"),
                line(
                    "Should Be Equal As Strings",
                    "${resp.status_code}",
                    expected or "200",
                ),
            )
        if method in ("POST", "PUT", "PATCH"):
            return out(
                line(f"${{resp}}=", method, locator, f"json={value or '{}'}", "expected_status=any"),
                line(
                    "Should Be Equal As Strings",
                    "${resp.status_code}",
                    expected or "200",
                ),
            )

    # ── DB（DatabaseLibrary）──────────────────────────
    if raw_action == "Db.Connect":
        # locator 不用；input 格式：driver|host|port|user|password|db
        parts = value.split("|")
        if len(parts) >= 6:
            driver, host, port, user, pwd, dbname = parts[:6]
            return [
                line(
                    "Connect To Database Using Custom Params",
                    driver,
                    f"database='{dbname}', user='{user}', password='{pwd}', host='{host}', port={port}",
                )
            ]
        return [line("Log", f"Db.Connect 參數格式錯誤：{value}")]
    if raw_action == "Db.Query":
        return [
            line("${rows}=", "Query", value or locator),
            line("Log", "${rows}"),
        ]
    if raw_action == "Db.Execute":
        return [line("Execute Sql String", value or locator)]
    if raw_action == "Db.RowCount":
        return [
            line("${cnt}=", "Row Count", value or locator),
            line("Should Be Equal As Integers", "${cnt}", expected or "1"),
        ]

    # ── Mobile（AppiumLibrary）────────────────────────
    if raw_action == "Mobile.Open":
        # value 為 capabilities JSON；locator 為 remote URL
        return [
            line(
                "Open Application",
                locator or "http://appium:4723/wd/hub",
                f"platformName={value or 'Android'}",
            )
        ]
    if raw_action == "Mobile.Click":
        return [line("Click Element", locator)]
    if raw_action == "Mobile.Input":
        return [line("Input Text", locator, value)]
    if raw_action == "Mobile.Tap":
        return [line("Tap", locator)]

    # 未識別
    return [line("Fail", f"Unknown action: {raw_action!r}")]


# ════════════════════════════════════════════════════════════════
# .robot 檔生成
# ════════════════════════════════════════════════════════════════


def _build_robot_file(
    steps: list[dict],
    ddt: Optional[dict],
    case_tag: str,
    screenshot_dir: str,
    headless: bool = False,
) -> tuple[str, list[list[dict]]]:
    """
    回傳 (.robot 檔內容, 每個 test case 的 step 清單)。
    每個 step 在 .robot 中會被包成：
        Log    AT_STEP idx=N
        Take Screenshot    filename=...    fullPage=False
        <action keyword(s)>
        Take Screenshot    filename=...    fullPage=False
    """
    rows = (ddt or {}).get("rows") or []
    headers = (ddt or {}).get("headers") or []
    if not rows:
        rows = [[]]

    lines: list[str] = []
    lines.append("*** Settings ***")
    lines.append("Library    Browser    auto_closing_level=TEST")
    lines.append("Library    RequestsLibrary")
    lines.append("Library    DatabaseLibrary")
    lines.append("Library    AppiumLibrary")
    lines.append("Library    Collections")
    lines.append("Library    OperatingSystem")
    lines.append("Library    String")
    lines.append("")
    # 保留 tag：讓所有 test 在 keyword 失敗後仍繼續執行剩餘步驟
    # （測試最終狀態仍會是 FAIL，但不會中斷後續 step）
    lines.append("Test Tags    robot:continue-on-failure")
    lines.append("")
    lines.append("*** Keywords ***")
    lines.append("Setup Browser Session")
    lines.append(f"    New Browser    chromium    headless={'true' if headless else 'false'}")
    lines.append("    New Context    viewport={'width': 1280, 'height': 720}")
    lines.append("    New Page")
    lines.append("")
    lines.append("Teardown Browser Session")
    lines.append("    Run Keyword And Ignore Error    Close Browser    ALL")
    lines.append("")

    lines.append("*** Test Cases ***")

    case_steps_per_row: list[list[dict]] = []

    for row_i, row in enumerate(rows):
        ctx = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}

        test_name = f"{case_tag}_row{row_i:02d}"
        lines.append(test_name)
        lines.append("    [Setup]    Setup Browser Session")
        lines.append("    [Teardown]    Teardown Browser Session")

        row_steps: list[dict] = []
        for step_i, step in enumerate(steps):
            global_idx = row_i * 1000 + step_i  # 與 caller 寫 ExecutionStepLog 的編碼一致
            row_steps.append(step)

            pre_path = os.path.join(screenshot_dir, f"{test_name}_s{step_i:02d}_pre")
            post_path = os.path.join(screenshot_dir, f"{test_name}_s{step_i:02d}_post")

            lines.append(f"    Log    AT_STEP idx={global_idx}")
            # 截圖以非 Browser action 包起來避免抓不到 page；對 Http/Db/Mobile 仍會嘗試但 keyword 失敗會被 ignore
            action_lower = (step.get("action") or "").strip().lower()
            is_browser = (
                not action_lower.startswith(("http.", "db.", "mobile."))
                and action_lower not in ("",)
            )
            step_locator = _rf_escape(_substitute(step.get("locator") or step.get("loc") or "", ctx))
            if is_browser:
                # 在 pre 截圖前先把目標元素以紅框 highlight，讓截圖直接含紅框標示
                if step_locator:
                    if _looks_named(step_locator):
                        lines.append(f"    ${{HL_LOC}}=    Set Variable    {step_locator}")
                        hl_loc = "${HL_LOC}"
                    else:
                        hl_loc = step_locator
                    # duration 只要夠 Take Screenshot 截到圖即可（800ms 已綽綽有餘）。
                    # 過長（例如 10s）會讓 robotframework-browser 注入的
                    # <div class="robotframework-browser-highlight"> 殘留在頁面上，
                    # 下一個 Click step 會被該 overlay 攔截而 timeout。
                    lines.append(
                        f"    Run Keyword And Ignore Error    Highlight Elements    {hl_loc}    duration=800ms    width=3px    style=solid    color=red"
                    )
                lines.append(
                    f"    Run Keyword And Ignore Error    Take Screenshot    filename={pre_path}    fullPage=False"
                )
                # 截圖完成後，主動把所有 robotframework-browser-highlight overlay 移除，
                # 以免下一步互動被它擋住（pointer-events 攔截）。
                lines.append(
                    "    Run Keyword And Ignore Error    Evaluate JavaScript    ${None}"
                    "    () => document.querySelectorAll('.robotframework-browser-highlight, .rfbrowser-highlight, .playwright-highlight').forEach(e => e.remove())"
                )
            translated = _translate_step(step, ctx)
            lines.extend(translated)
            if is_browser:
                lines.append(
                    f"    Run Keyword And Ignore Error    Take Screenshot    filename={post_path}    fullPage=False"
                )

        lines.append("")
        case_steps_per_row.append(row_steps)

    return "\n".join(lines) + "\n", case_steps_per_row


# ════════════════════════════════════════════════════════════════
# 對外 API：給 Celery task 呼叫
# ════════════════════════════════════════════════════════════════


def run_testcase(
    steps: list[dict],
    ddt: Optional[dict],
    report_id: str,
    case_tag: str,
    publish_log: Callable[[str, str], None],
    headless: bool = True,
) -> list[CaseResult]:
    """
    執行單一測試案例（含 DDT 多列）。
    回傳每一輪 (DDT 列) 的 CaseResult；無 DDT 時長度為 1。
    """
    if not steps:
        publish_log("ERROR", "  ⚠ 此案例 steps_json 為空")
        return [CaseResult(passed=False, steps=[], duration_ms=0)]

    # ── 截圖目錄與工作區 ──────────────────────────────
    screenshot_dir = os.path.abspath(os.path.join(settings.PIC_FOLDER, report_id))
    os.makedirs(screenshot_dir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix=f"rf_{case_tag}_")
    robot_file = os.path.join(workdir, "test.robot")
    output_dir = os.path.join(workdir, "out")
    os.makedirs(output_dir, exist_ok=True)
    result_json = os.path.join(workdir, "step_results.json")

    # ── 產生 .robot ────────────────────────────────────
    robot_text, _ = _build_robot_file(steps, ddt, case_tag, screenshot_dir, headless=headless)
    with open(robot_file, "w", encoding="utf-8") as f:
        f.write(robot_text)
    publish_log("INFO", f"  📝 已生成 {os.path.basename(robot_file)} ({len(steps)} 步驟)")

    # ── 構建環境變數（傳給 listener）──────────────────
    env = os.environ.copy()
    env["AUTOTEST_REDIS_URL"] = settings.REDIS_URL
    env["AUTOTEST_LOG_CHANNEL"] = f"task:{_extract_task_id(publish_log) or report_id}:logs"
    env["AUTOTEST_RESULT_PATH"] = result_json
    env["AUTOTEST_SCREENSHOT_URL_PREFIX"] = f"{settings.BASE_URL}/pics/{report_id}"
    if not headless:
        env["AUTOTEST_HEADLESS"] = "0"

    # ── subprocess 跑 robot ────────────────────────────
    cmd = [
        sys.executable, "-m", "robot",
        "--listener", "tasks.robot_listener.RTListener",
        "--outputdir", output_dir,
        "--loglevel", "INFO",
        robot_file,
    ]

    case_start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),  # backend/
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        publish_log("ERROR", "  ⏱ Robot 執行逾時 (600s)")
        _safe_cleanup(workdir)
        return [CaseResult(passed=False, steps=[], duration_ms=600_000)]
    except FileNotFoundError as e:
        publish_log("ERROR", f"  💥 找不到 robot 執行檔: {e}")
        _safe_cleanup(workdir)
        return [CaseResult(passed=False, steps=[], duration_ms=0)]

    case_dur = int((time.time() - case_start) * 1000)
    rc = proc.returncode

    # ── 解析 listener 輸出的 step 結果 ────────────────
    step_records: list[dict] = []
    if os.path.isfile(result_json):
        try:
            with open(result_json, "r", encoding="utf-8") as f:
                step_records = json.load(f)
        except Exception as e:
            publish_log("ERROR", f"  ⚠ 解析 listener JSON 失敗: {e}")

    if not step_records:
        # listener 沒寫東西 → 表示 robot 啟動失敗
        publish_log("ERROR", f"  💥 Robot 未產生結果（rc={rc}）")
        if proc.stderr:
            publish_log("ERROR", proc.stderr.strip()[:500])
        _safe_cleanup(workdir)
        return [CaseResult(passed=False, steps=[], duration_ms=case_dur)]

    # ── 把 step_records 依 DDT row 分組 ────────────────
    rows = (ddt or {}).get("rows") or []
    n_rows = max(1, len(rows))
    n_steps = len(steps)

    case_results: list[CaseResult] = []
    for row_i in range(n_rows):
        row_steps: list[StepResult] = []
        row_passed = True
        row_dur = 0
        test_name = f"{case_tag}_row{row_i:02d}"
        for step_i in range(n_steps):
            global_idx = row_i * 1000 + step_i
            rec = next((r for r in step_records if r.get("step_index") == global_idx), None)

            # 直接由預定檔名補回截圖 URL（listener 解析 result.message 並不可靠）
            pre_url = (rec or {}).get("pre") or _resolve_screenshot_url(
                screenshot_dir, f"{test_name}_s{step_i:02d}_pre", report_id
            )
            post_url = (rec or {}).get("post") or _resolve_screenshot_url(
                screenshot_dir, f"{test_name}_s{step_i:02d}_post", report_id
            )

            if rec is None:
                # 該步驟未執行（前面失敗中止）
                row_steps.append(
                    StepResult(
                        status="SKIPPED",
                        duration_ms=0,
                        error_message=None,
                        pre_screenshot_url=pre_url,
                        post_screenshot_url=post_url,
                        target_highlight_json=None,
                    )
                )
                continue
            status = rec.get("status", "FAILED")
            row_dur += rec.get("duration_ms", 0)
            if status != "PASSED":
                row_passed = False
            row_steps.append(
                StepResult(
                    status=status,
                    duration_ms=rec.get("duration_ms", 0),
                    error_message=rec.get("error"),
                    pre_screenshot_url=pre_url,
                    post_screenshot_url=post_url,
                    target_highlight_json=None,
                )
            )
        case_results.append(CaseResult(passed=row_passed, steps=row_steps, duration_ms=row_dur))

    _safe_cleanup(workdir)
    return case_results


# ════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════


def _safe_cleanup(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# Browser library Take Screenshot 會在 filename 後自動加副檔名（通常 .png），
# 但版本不同有時會加上 timestamp / 編號。此 helper 依「base 名稱」搜尋實際檔案，
# 並轉為對外可存取 URL（minio 或本地 /pics）。
_SCREENSHOT_EXT_CANDIDATES = (".png", ".jpeg", ".jpg")


def _resolve_screenshot_url(
    screenshot_dir: str, base_name: str, report_id: str
) -> Optional[str]:
    candidate: Optional[str] = None
    # 直接命中
    for ext in _SCREENSHOT_EXT_CANDIDATES:
        path = os.path.join(screenshot_dir, base_name + ext)
        if os.path.isfile(path):
            candidate = path
            break
    # 退而求其次：前綴比對（Browser library 可能加 _1 / timestamp）
    if candidate is None and os.path.isdir(screenshot_dir):
        try:
            matches = sorted(
                fn for fn in os.listdir(screenshot_dir)
                if fn.startswith(base_name) and fn.lower().endswith(_SCREENSHOT_EXT_CANDIDATES)
            )
            if matches:
                candidate = os.path.join(screenshot_dir, matches[-1])
        except OSError:
            return None
    if candidate is None:
        return None

    # 轉成 URL
    try:
        if (settings.STORAGE_BACKEND or "local").lower() == "minio":
            from app.services.storage_service import save_bytes  # type: ignore

            with open(candidate, "rb") as fh:
                data = fh.read()
            key = f"screenshots/{report_id}/{os.path.basename(candidate)}"
            return save_bytes(data, key, bucket="results", content_type="image/png")
    except Exception:
        pass

    rel = os.path.basename(candidate)
    base_url = (settings.BASE_URL or "").rstrip("/")
    return f"{base_url}/pics/{report_id}/{rel}"


def _extract_task_id(publish_log: Callable) -> Optional[str]:
    """
    publish_log 是 caller 提供的 closure，本身無法直接拿到 task_id。
    為了讓 listener 用同一條 channel，caller 在環境變數 AUTOTEST_TASK_ID 提供。
    """
    return os.environ.get("AUTOTEST_TASK_ID")
