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
    # 比對條件（compare）用於 Assert* 動作決定使用哪個 Should* keyword
    compare = (step.get("compare") or "").strip()

    def line(*parts: str) -> str:
        return "    " + "    ".join(p for p in parts)

    def compare_line(actual: str, cmp: str, exp: str, numeric: bool = False) -> str:
        """依 compare 條件產生單行 Should* 斷言。numeric=True 時走數值比較。"""
        cmp = (cmp or "Equals")
        eq_kw = "Should Be Equal As Integers" if numeric else "Should Be Equal As Strings"
        neq_kw = "Should Not Be Equal As Integers" if numeric else "Should Not Be Equal As Strings"
        mapping = {
            "Equals": (eq_kw, actual, exp),
            "NotEquals": (neq_kw, actual, exp),
            "Contains": ("Should Contain", actual, exp),
            "NotContains": ("Should Not Contain", actual, exp),
            "StartsWith": ("Should Start With", actual, exp),
            "EndsWith": ("Should End With", actual, exp),
            "Regex": ("Should Match Regexp", actual, exp),
            "GreaterThan": ("Should Be True", f"{actual} > {exp}"),
            "LessThan": ("Should Be True", f"{actual} < {exp}"),
            # 狀態型比較只用在 state-based 斷言（AssertVisible/Hidden/Checked），
            # 不應走到這裡；若誤用則退回 Equals。
        }
        parts = mapping.get(cmp) or mapping["Equals"]
        return line(*parts)

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
    # 導航
    if action in ("goto", "navigate", "open"):
        target = value or expected or locator
        return out(line("Go To", target))
    if action == "reload":
        return out(line("Reload"))
    if action == "goback":
        return out(line("Go Back"))
    if action == "goforward":
        return out(line("Go Forward"))

    # 點擊 / 輸入
    # ★ 所有 Click 動作之前都先 Wait For Elements State ... visible timeout=10s，
    #   避免目標元素找不到時整個 test 卡住（Browser Library 的 Click 預設 timeout 會跟隨 suite timeout，
    #   在舊版 .robot 裡可能是 30s～無上限）。
    if action == "click":
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Click", locator),
        )
    if action in ("doubleclick", "dblclick"):
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Click", locator, "clickCount=2"),
        )
    if action == "rightclick":
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Click", locator, "button=right"),
        )
    if action in ("fill", "input"):
        return out(line("Fill Text", locator, value))
    if action == "type":
        return out(line("Type Text", locator, value))
    if action == "clear":
        return out(line("Clear Text", locator))
    if action == "press":
        return out(line("Press Keys", locator, value or "Enter"))
    if action == "hover":
        return out(line("Hover", locator))
    if action == "focus":
        return out(line("Focus", locator))
    if action == "check":
        return out(line("Check Checkbox", locator))
    if action == "uncheck":
        return out(line("Uncheck Checkbox", locator))
    if action == "select":
        return out(line("Select Options By", locator, "value", value))
    if action == "upload":
        # value = 檔案路徑（容器內可讀）
        return out(line("Upload File By Selector", locator, value))
    if action == "download":
        # 下載檔案：locator = 觸發下載的連結/按鈕；value = 儲存到 worker 容器內的檔案路徑
        # 使用 Browser Library 的 Promise / Wait For 模式：先下 promise，再點擊，再等它完成
        save_path = value or "/tmp/download"
        return out(
            line("${dl}=", "Promise To Wait For Download", save_path),
            line("Click", locator),
            line("${downloaded}=", "Wait For", "${dl}"),
            line("File Should Exist", save_path),
        )
    if action in ("clickat", "canvasclick"):
        # 在元素的指定座標點擊（適合 Canvas / 地圖類互動）
        # value 格式："x,y" 或 "x y"，單位為元素內部像素（左上角 0,0）
        import re as _re
        parts = _re.split(r"[,\s]+", (value or "").strip())
        parts = [p for p in parts if p]
        x, y = (parts + ["0", "0"])[:2]
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Click With Options", locator, f"position_x={x}", f"position_y={y}"),
        )

    # 捲動 / 拖曳
    if action == "scroll":
        # value 格式："x y"（像素）或單一數字（垂直捲動量）
        # Browser Library 簽名：Scroll By  selector  vertical  horizontal  behavior=
        parts = (value or "0 0").split()
        if len(parts) == 1:
            dx, dy = "0", parts[0]
        else:
            dx, dy = parts[0], parts[1]
        return out(line("Scroll By", "${None}", f"vertical={dy}", f"horizontal={dx}"))
    if action == "scrolltoelement":
        return out(line("Scroll To Element", locator))
    if action == "draganddrop":
        # locator = 來源；value = 目標
        return out(line("Drag And Drop", locator, value))

    # 等待 / 截圖 / 分頁 / JS
    if action in ("wait", "sleep"):
        ms = value or expected or "1000"
        return out(line("Sleep", f"{int(float(ms)) / 1000.0}s"))
    if action in ("waitforselector", "waitfor"):
        return out(line("Wait For Elements State", locator, value or "visible"))
    if action == "waitforloadstate":
        return out(line("Wait For Load State", value or "load"))
    if action == "screenshot":
        # 系統已在每步前後自動截圖，這裡用於「使用者在步驟內手動再拍一張」
        fname = value or "user_screenshot"
        return out(line("Take Screenshot", f"filename={fname}"))
    if action == "switchtab":
        # value = 目標 index / id，預設 NEW
        return out(line("Switch Page", value or "NEW"))
    if action == "closetab":
        return out(line("Close Page"))
    if action == "executescript":
        # value = JS 片段；locator 可作為 element 參數（可空）
        if locator:
            return out(line("${result}=", "Evaluate JavaScript", locator, value))
        return out(line("${result}=", "Evaluate JavaScript", "${None}", value))

    # 斷言
    if action in ("assertvisible", "shouldbevisible"):
        return out(line("Wait For Elements State", locator, "visible"))
    if action in ("asserthidden", "shouldbehidden"):
        return out(line("Wait For Elements State", locator, "hidden"))
    if action == "assertchecked":
        return out(
            line("${state}=", "Get Checkbox State", locator),
            line("Should Be True", "${state}"),
        )
    if action == "assertenabled":
        return out(line("Wait For Elements State", locator, "enabled"))
    if action == "assertdisabled":
        return out(line("Wait For Elements State", locator, "disabled"))
    if action == "asserttext":
        # 文字比對預設使用 Contains（比 Equals 實用）
        return out(
            line("${actual}=", "Get Text", locator),
            compare_line("${actual}", compare or "Contains", expected),
        )
    if action == "assertvalue":
        return out(
            line("${actual}=", "Get Property", locator, "value"),
            compare_line("${actual}", compare or "Equals", expected),
        )
    if action == "asserturl":
        return out(
            line("${url}=", "Get Url"),
            compare_line("${url}", compare or "Contains", expected),
        )
    if action == "asserttitle":
        return out(
            line("${title}=", "Get Title"),
            compare_line("${title}", compare or "Contains", expected),
        )
    if action == "assertcount":
        return out(
            line("${cnt}=", "Get Element Count", locator),
            compare_line("${cnt}", compare or "Equals", expected or "1", numeric=True),
        )
    if action == "assertattribute":
        # value = 屬性名；expected = 期望值
        return out(
            line("${attr}=", "Get Attribute", locator, value or "value"),
            compare_line("${attr}", compare or "Equals", expected),
        )
    if action == "assertimageloaded":
        # 檢查 <img> 是否真的載入完成（complete=true && naturalWidth>0），
        # 避免破圖被當成「顯示」通過
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line(
                "${loaded}=",
                "Evaluate JavaScript",
                locator,
                "(el) => el && el.complete && el.naturalWidth > 0",
            ),
            line("Should Be True", "${loaded}"),
        )
    if action == "assertboundingbox":
        # value = 期望的 x,y,w,h（逗號/空白分隔）；expected = 允許誤差（像素，預設 2）
        # 例如：value="100,200,300,400"  expected="3"
        import re as _re
        parts = _re.split(r"[,\s]+", (value or "").strip())
        parts = [p for p in parts if p]
        if len(parts) < 4:
            return out(
                line("Log", f"AssertBoundingBox 需 value='x,y,w,h'，收到：{value!r}")
            )
        ex, ey, ew, eh = parts[:4]
        tol = expected or "2"
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("${bb}=", "Get Boundingbox", locator),
            line("Should Be True", f"abs(${{bb}}[\"x\"] - {ex}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"y\"] - {ey}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"width\"] - {ew}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"height\"] - {eh}) <= {tol}"),
        )

    # ── HTTP（RequestsLibrary）────────────────────────
    if raw_action.startswith("Http."):
        sub = raw_action.split(".", 1)[1]
        sub_up = sub.upper()

        # 設定 header / base url / auth（存於 suite 變數，供後續請求使用）
        if sub == "SetHeader":
            # locator = header 名稱；value = header 值
            return out(line("Set To Dictionary", "${HTTP_HEADERS}", locator, value))
        if sub == "SetBaseURL":
            return out(line("Set Suite Variable", "${HTTP_BASE_URL}", value or locator))
        if sub == "SetAuth":
            # value = "user:pass"（Basic Auth）或單純 token
            return out(line("Set Suite Variable", "${HTTP_AUTH}", value or locator))

        # 發送請求（locator = path/URL；value = json body；expected = 預期 status code，選填）
        # 請求結果固定存入 ${HTTP_RESP} 供後續 Assert* / ExtractJson / SaveToken 使用
        http_methods = {"GET", "DELETE", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"}
        if sub_up in http_methods:
            url_expr = "${HTTP_BASE_URL}" + (locator or "")
            body_rows: list[str] = []
            if sub_up in ("GET", "DELETE", "HEAD", "OPTIONS"):
                body_rows.append(
                    line("${HTTP_RESP}=", sub_up, url_expr, "headers=${HTTP_HEADERS}", "expected_status=any")
                )
            else:
                body_rows.append(
                    line(
                        "${HTTP_RESP}=",
                        sub_up,
                        url_expr,
                        f"json={value or '{}'}",
                        "headers=${HTTP_HEADERS}",
                        "expected_status=any",
                    )
                )
            body_rows.append(line("Set Suite Variable", "${HTTP_RESP}"))
            # 僅在使用者有填 expected 時才加上 status 斷言（於產生階段判斷，避免 .robot 內動態條件）
            if expected:
                body_rows.append(
                    line("Should Be Equal As Strings", "${HTTP_RESP.status_code}", expected)
                )
            return out(body_rows)

        # 從上一次回應抽出 JSON / 儲存 token
        # ★ 透過 `Evaluate` 並用 `$HTTP_RESP`（無大括號形式）在 Python 表達式中引用 Robot 變數
        if sub == "ExtractJson":
            # locator = JSON key/點路徑；value = 儲存到的變數名（不含 $）；預設 EXTRACTED
            var_name = value or "EXTRACTED"
            return out(
                line(f"${{{var_name}}}=", "Evaluate", f"$HTTP_RESP.json().get('{locator}', '')"),
                line("Set Suite Variable", f"${{{var_name}}}"),
            )
        if sub == "SaveToken":
            # locator = JSON key（例 token / access_token）；存成 ${TOKEN}
            key = locator or "token"
            return out(
                line("${TOKEN}=", "Evaluate", f"$HTTP_RESP.json().get('{key}', '')"),
                line("Set Suite Variable", "${TOKEN}"),
            )

        # 斷言
        if sub == "AssertStatus":
            return out(
                compare_line(
                    "${HTTP_RESP.status_code}",
                    compare or "Equals",
                    expected or "200",
                    numeric=True,
                ),
            )
        if sub == "AssertJsonValue":
            # locator = JSON key/路徑（點記法）；expected = 預期值
            return out(
                line("${actual}=", "Evaluate", f"$HTTP_RESP.json().get('{locator}', '')"),
                compare_line("${actual}", compare or "Equals", expected),
            )
        if sub == "AssertHeader":
            # locator = header 名稱；expected = 預期值
            return out(
                line("${hv}=", "Evaluate", f"$HTTP_RESP.headers.get('{locator}', '')"),
                compare_line("${hv}", compare or "Contains", expected),
            )
        if sub == "AssertBodyContains":
            return out(
                line("${body}=", "Evaluate", "$HTTP_RESP.text"),
                compare_line("${body}", compare or "Contains", expected),
            )
        if sub == "AssertResponseTime":
            # expected = 毫秒上限；預設比較語意為 LessThan
            return out(
                line(
                    "${elapsed_ms}=",
                    "Evaluate",
                    "$HTTP_RESP.elapsed.total_seconds() * 1000",
                ),
                compare_line("${elapsed_ms}", compare or "LessThan", expected or "2000", numeric=True),
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
        # value 為 platformName / capabilities；locator 為 remote URL
        return out(
            line(
                "Open Application",
                locator or "http://appium:4723/wd/hub",
                f"platformName={value or 'Android'}",
            )
        )
    if raw_action == "Mobile.Close":
        return out(line("Close Application"))
    if raw_action == "Mobile.Click":
        return out(line("Click Element", locator))
    if raw_action == "Mobile.Tap":
        return out(line("Tap", locator))
    if raw_action == "Mobile.DoubleTap":
        return out(line("Tap", locator, "count=2"))
    if raw_action == "Mobile.LongPress":
        return out(line("Long Press", locator, f"duration={value or '1000'}"))
    if raw_action == "Mobile.Input":
        return out(line("Input Text", locator, value))
    if raw_action == "Mobile.Clear":
        return out(line("Clear Text", locator))
    if raw_action == "Mobile.Swipe":
        # value 格式：startX startY offsetX offsetY（AppiumLibrary 使用 offset）
        parts = (value or "0 0 0 0").split()
        sx, sy, ox, oy = (parts + ["0", "0", "0", "0"])[:4]
        return out(line("Swipe", sx, sy, ox, oy))
    if raw_action == "Mobile.SwipeUp":
        return out(line("Swipe By Percent", "50", "80", "50", "20"))
    if raw_action == "Mobile.SwipeDown":
        return out(line("Swipe By Percent", "50", "20", "50", "80"))
    if raw_action == "Mobile.SwipeLeft":
        return out(line("Swipe By Percent", "80", "50", "20", "50"))
    if raw_action == "Mobile.SwipeRight":
        return out(line("Swipe By Percent", "20", "50", "80", "50"))
    if raw_action == "Mobile.Press":
        # value = keycode（整數）
        return out(line("Press Keycode", value or "66"))
    if raw_action == "Mobile.PressBack":
        return out(line("Press Keycode", "4"))
    if raw_action == "Mobile.PressHome":
        return out(line("Press Keycode", "3"))
    if raw_action == "Mobile.Wait":
        return out(line("Wait Until Element Is Visible", locator, f"timeout={value or '10'}"))
    if raw_action == "Mobile.Screenshot":
        return out(line("Capture Page Screenshot"))
    if raw_action == "Mobile.HideKeyboard":
        return out(line("Hide Keyboard"))
    if raw_action == "Mobile.AssertVisible":
        return out(line("Element Should Be Visible", locator))
    if raw_action == "Mobile.AssertText":
        return out(line("Element Should Contain Text", locator, expected))
    if raw_action == "Mobile.AssertEnabled":
        return out(line("Element Should Be Enabled", locator))

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
    # Http.* 動作所使用的共用 suite 變數（SetHeader / SetBaseURL / SetAuth 寫入；
    # GET/POST/... 使用；AssertStatus / ExtractJson / SaveToken 讀取 HTTP_RESP）
    lines.append("*** Variables ***")
    lines.append("&{HTTP_HEADERS}")
    lines.append("${HTTP_BASE_URL}    ${EMPTY}")
    lines.append("${HTTP_AUTH}    ${EMPTY}")
    lines.append("${HTTP_RESP}    ${None}")
    lines.append("")
    lines.append("*** Keywords ***")
    lines.append("Setup Browser Session")
    lines.append(f"    New Browser    chromium    headless={'true' if headless else 'false'}")
    lines.append("    New Context    viewport={'width': 1280, 'height': 720}")
    lines.append("    New Page")
    # 預設所有 Browser Library 動作（Click / Fill / Wait For Elements State / ...）
    # 超過 30 秒就算失敗，避免找不到元素時整個 test 卡住無限等。
    lines.append("    Set Browser Timeout    30s")
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
