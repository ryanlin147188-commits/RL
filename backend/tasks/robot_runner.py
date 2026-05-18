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

import ast
import json
import operator
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
    # 該步驟錄影切片（.webm）對外 URL；未啟用 enable_recording 或 ffmpeg 不可用時為 None。
    step_video_url: Optional[str] = None
    # ── Screenshot Diff 欄位（只 AssertScreenshotMatch step 才會有）──
    screenshot_baseline_url: Optional[str] = None
    screenshot_diff_url: Optional[str] = None
    screenshot_diff_pct: Optional[float] = None


@dataclass
class CaseResult:
    passed: bool
    steps: list[StepResult]
    duration_ms: int
    # 案例級錄影 / 軌跡（一個案例 = 一輪 DDT 行；每輪各一份）。
    # 未啟用 enable_recording 時兩者皆為 None。
    trace_url: Optional[str] = None
    video_url: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# 變數替換 + Robot 字串跳脫
# ════════════════════════════════════════════════════════════════

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Sprint 1 — mini DSL:`{{= 表達式 | filter | filter }}`
# 例:
#   {{= ${count} + 1 }}                算術:把 count 加 1
#   {{= ${name} | upper }}             pipe filter:轉大寫
#   {{= ${rows} | len }}               長度
#   {{= ${api_resp} | json:id }}       從 JSON 字串取 path "id"
# 不支援函式呼叫 / 屬性存取(防 injection),只允許算術 / 比較 / literal。
_EXPR_PATTERN = re.compile(r"\{\{=(.+?)\}\}", re.DOTALL)

_AST_OPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.UAdd: operator.pos, ast.USub: operator.neg,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.And: lambda a, b: a and b, ast.Or: lambda a, b: a or b, ast.Not: operator.not_,
}


def _safe_eval(expr: str, names: dict | None = None):
    """只允許算術 / 比較 / 邏輯運算 / literal / 已知變數;不允許函式呼叫 / 屬性存取。"""
    names = names or {}
    tree = ast.parse(expr, mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            if node.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[node.id]
            raise ValueError(f"未知變數:{node.id}")
        if isinstance(node, ast.BinOp):
            op = _AST_OPS.get(type(node.op))
            if not op:
                raise ValueError(f"不允許的運算子:{type(node.op).__name__}")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _AST_OPS.get(type(node.op))
            if not op:
                raise ValueError(f"不允許的一元運算子:{type(node.op).__name__}")
            return op(_eval(node.operand))
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op_node, right_node in zip(node.ops, node.comparators):
                op = _AST_OPS.get(type(op_node))
                if not op:
                    raise ValueError(f"不允許的比較運算子:{type(op_node).__name__}")
                right = _eval(right_node)
                if not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            op = _AST_OPS.get(type(node.op))
            if not op:
                raise ValueError("不允許的布林運算")
            vals = [_eval(v) for v in node.values]
            result = vals[0]
            for v in vals[1:]:
                result = op(result, v)
            return result
        if isinstance(node, ast.List):
            return [_eval(el) for el in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(el) for el in node.elts)
        raise ValueError(f"不允許的語法:{type(node).__name__}")

    return _eval(tree)


def _apply_filter(value: Any, spec: str) -> Any:
    """套單個 filter,支援帶參數(json:path)。"""
    spec = spec.strip()
    if not spec:
        return value
    name, _, arg = spec.partition(":")
    name = name.strip()
    arg = arg.strip()
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "strip":
        return str(value).strip()
    if name == "len":
        try:
            return len(value)
        except TypeError:
            return len(str(value))
    if name == "int":
        return int(value)
    if name == "float":
        return float(value)
    if name == "str":
        return str(value)
    if name == "json":
        # 把 value 當 JSON 字串解析,然後取 arg 為 dotted path
        try:
            data = value if not isinstance(value, str) else json.loads(value)
        except (ValueError, json.JSONDecodeError):
            return ""
        if not arg:
            return data
        cur = data
        for part in arg.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                idx = int(part)
                cur = cur[idx] if 0 <= idx < len(cur) else None
            else:
                cur = None
            if cur is None:
                break
        return cur if cur is not None else ""
    # 未知 filter:回原值
    return value


def _eval_dsl(body: str, ctx: dict) -> str:
    """評估 {{= ... }} 內容。先把 ${var} 變成 _safe_eval 的 names dict,再套 pipe filters。"""
    # 切 main expr 跟 filters
    parts = body.split("|")
    main = parts[0].strip()
    filters = [p.strip() for p in parts[1:] if p.strip()]

    # 收集 main 內用到的變數,把它們轉成合法 Python 識別字 + 餵給 _safe_eval
    # 例:`${count} + 1` → 替換成 `_v_count + 1`,names = {"_v_count": ctx["count"]}
    names: dict[str, Any] = {}
    def _var_to_ident(m: re.Match) -> str:
        key = m.group(1) or m.group(2)
        if key not in ctx and f"${key}" not in ctx:
            return m.group(0)  # 找不到 → 保留原樣讓下方 ast.parse 報錯
        val = ctx.get(key) if key in ctx else ctx.get(f"${key}")
        # 嘗試把字串值轉成數值;失敗保留字串
        try:
            num = float(val) if "." in str(val) else int(val)
            val_for_expr = num
        except (ValueError, TypeError):
            val_for_expr = val
        ident = f"_v_{re.sub(r'[^A-Za-z0-9_]', '_', str(key))}"
        names[ident] = val_for_expr
        return ident

    py_expr = _VAR_PATTERN.sub(_var_to_ident, main)

    # 算 main expr
    try:
        result = _safe_eval(py_expr, names) if py_expr.strip() else ""
    except Exception as e:
        # DSL 評估失敗 → 保留原 raw 字串(避免整個 step 爆掉)
        return f"<<DSL error: {e}>>"

    # 套 filters
    for f in filters:
        try:
            result = _apply_filter(result, f)
        except Exception:
            pass

    return str(result)


def _substitute(text: Any, ctx: dict) -> str:
    """將 ${var} / $var / {{= 表達式 }} 用 ctx 取代;非字串轉成空字串。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    # Step 1: 先處理 mini DSL `{{= ... }}`(可內含 ${var})
    text = _EXPR_PATTERN.sub(lambda m: _eval_dsl(m.group(1), ctx), text)

    # Step 2: 處理單純的 ${var} / $var(沒包在 DSL 內的)
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

# Click 前的「清除 overlay」JS:在大多數 SPA / UI framework 下涵蓋常見場景:
#   * Bootstrap / MUI / AntD / SweetAlert / Offcanvas 等 modal backdrop
#   * Body / html 鎖捲動的 class(modal-open / sidebar-open / no-scroll)
#   * App-sidebar(metismenu 等)/ drawer overlay / Toast 通知
# 全部寫成單行 JS,讓 Robot Framework 用 4-space tokenize 時當成單一參數。
_OVERLAY_CLEANUP_JS = (
    "() => {"
    "['modal-open','sidebar-open','sidebar-mobile-open','no-scroll','overflow-hidden']"
    ".forEach(c => { document.body.classList.remove(c); document.documentElement.classList.remove(c); });"
    "document.querySelectorAll('.modal-backdrop,.MuiBackdrop-root,.ant-modal-mask,.ant-modal-wrap,"
    ".swal2-container,.popover-backdrop,.offcanvas-backdrop,.toast-container,.cdk-overlay-backdrop')"
    ".forEach(el => { try { el.remove(); } catch(e) {} });"
    "document.querySelectorAll('.app-sidebar,.sidebar-shadow,.sidebar-overlay,.drawer-backdrop,"
    ".metismenu-overlay,[data-overlay-dismiss]')"
    ".forEach(el => { el.style.pointerEvents = 'none'; });"
    "document.querySelectorAll('[role=\"dialog\"][aria-modal=\"true\"] [aria-label*=\"close\" i],"
    "[role=\"dialog\"][aria-modal=\"true\"] [aria-label*=\"關閉\"],"
    ".modal.show [data-bs-dismiss=\"modal\"],.modal.in [data-dismiss=\"modal\"]')"
    ".forEach(el => { try { el.click(); } catch(e) {} });"
    "}"
)

# 強化版：額外清除 role=menu/listbox/tooltip 並 blur activeElement
# 僅供 CloseOverlay action 使用，不替換現有 _OVERLAY_CLEANUP_JS（避免影響既有 Click 行為）
_OVERLAY_CLEANUP_JS_ENHANCED = (
    "() => {"
    "['modal-open','sidebar-open','sidebar-mobile-open','no-scroll','overflow-hidden']"
    ".forEach(c => { document.body.classList.remove(c); document.documentElement.classList.remove(c); });"
    "document.querySelectorAll('.modal-backdrop,.MuiBackdrop-root,.ant-modal-mask,.ant-modal-wrap,"
    ".swal2-container,.popover-backdrop,.offcanvas-backdrop,.toast-container,.cdk-overlay-backdrop')"
    ".forEach(el => { try { el.remove(); } catch(e) {} });"
    "document.querySelectorAll('.app-sidebar,.sidebar-shadow,.sidebar-overlay,.drawer-backdrop,"
    ".metismenu-overlay,[data-overlay-dismiss]')"
    ".forEach(el => { el.style.pointerEvents = 'none'; });"
    "document.querySelectorAll('[role=\"dialog\"][aria-modal=\"true\"] [aria-label*=\"close\" i],"
    "[role=\"dialog\"][aria-modal=\"true\"] [aria-label*=\"關閉\"],"
    ".modal.show [data-bs-dismiss=\"modal\"],.modal.in [data-dismiss=\"modal\"]')"
    ".forEach(el => { try { el.click(); } catch(e) {} });"
    "document.querySelectorAll('[role=\"menu\"],[role=\"listbox\"],[role=\"tooltip\"]')"
    ".forEach(el => { try { el.remove(); } catch(e) {} });"
    "try { document.activeElement?.blur(); } catch(e) {}"
    "}"
)


def _overlay_cleanup_line() -> str:
    """產生「點 click 前先收掉常見 overlay」的 Robot 行;Run Keyword And Ignore Error
    包起來,失敗也不擋下一步,只是 best-effort 清乾淨點擊區。"""
    return "    " + "    ".join([
        "Run Keyword And Ignore Error",
        "Evaluate JavaScript",
        "${None}",
        _OVERLAY_CLEANUP_JS,
    ])


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

    def out_w(*body: list[str] | str) -> list[str]:
        """跟 out() 一樣,但先 prepend 一條 Wait For Elements State(60s)。
        Browser Library 的 Get Text / Fill Text / Get Property 等 keyword 預設
        不會等元素出現,SPA 場景常導致瞬間 fail。對於有 locator 的讀寫動作,
        強制先等元素 visible 比較穩。沒有 locator 的呼叫端不要用這個 helper。
        """
        if not locator:
            return out(*body)
        wait = line("Wait For Elements State", locator, "visible", "timeout=20s")
        return out(wait, *body)

    # ── Browser Library（預設）────────────────────────
    # 導航
    if action in ("goto", "navigate", "open"):
        target = value or expected or locator
        # wait_until=domcontentloaded:DOM 解析完就返回,不等 page 上所有 XHR /
        # image / 第三方 script 都載完才繼續(預設 'load' 在 SPA 內常因某個慢
        # XHR 而 hang)。timeout=30s 明確上限,避免被 docker-socket-proxy 600s
        # idle limit 攔成奇怪的容器 timeout。
        return out(line("Go To", target, "wait_until=domcontentloaded", "timeout=30s"))
    if action == "reload":
        return out(line("Reload"))
    if action == "goback":
        return out(line("Go Back"))
    if action == "goforward":
        return out(line("Go Forward"))

    # 點擊 / 輸入
    # ★ Click 前的隱性等待 + 自動關閉 overlay:
    #   1) Run JS 清掉常見的 modal backdrop / drawer / sidebar overlay / toast,
    #      避免 Playwright 因為「目標元素上面有東西蓋住」而拒絕 click。
    #   2) Wait For Elements State stable timeout=20s — 等元素 visible 且 200ms
    #      內不再位移(動畫穩了)才動手。
    #   3) Click 走正常路徑(不用 force=True;這版 Browser Library 的 Click 也
    #      不支援 force kwarg)。
    if action == "click":
        return out(
            _overlay_cleanup_line(),
            line("Wait For Elements State", locator, "stable", "timeout=20s"),
            line("Click", locator),
        )
    if action in ("doubleclick", "dblclick"):
        return out(
            _overlay_cleanup_line(),
            line("Wait For Elements State", locator, "stable", "timeout=20s"),
            line("Click", locator, "clickCount=2"),
        )
    if action == "rightclick":
        return out(
            _overlay_cleanup_line(),
            line("Wait For Elements State", locator, "stable", "timeout=20s"),
            line("Click", locator, "button=right"),
        )
    if action in ("fill", "input"):
        # 預設只填字;若 expected 有值,fill 完再 Get Property value 比對,
        # 讓使用者在 UI 設的 compare+expected 真的會 fail(原本被吞掉導致一律 pass)。
        body = [line("Fill Text", locator, value)]
        if expected:
            body.append(line("${actual}=", "Get Property", locator, "value"))
            body.append(compare_line("${actual}", compare or "Equals", expected))
        return out_w(*body)
    if action == "type":
        body = [line("Type Text", locator, value)]
        if expected:
            body.append(line("${actual}=", "Get Property", locator, "value"))
            body.append(compare_line("${actual}", compare or "Equals", expected))
        return out_w(*body)
    if action == "clear":
        return out_w(line("Clear Text", locator))
    if action == "press":
        return out_w(line("Press Keys", locator, value or "Enter"))
    if action == "hover":
        return out_w(line("Hover", locator))
    if action == "focus":
        return out_w(line("Focus", locator))
    if action == "check":
        return out_w(line("Check Checkbox", locator))
    if action == "uncheck":
        return out_w(line("Uncheck Checkbox", locator))
    if action == "select":
        return out_w(line("Select Options By", locator, "value", value))
    if action == "upload":
        # value = 檔案路徑（容器內可讀）
        return out_w(line("Upload File By Selector", locator, value))
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
            line("Wait For Elements State", locator, "visible", "timeout=20s"),
            line("Click With Options", locator, f"position_x={x}", f"position_y={y}"),
        )

    # ── 遮擋處理：關閉覆蓋層 / 強制點擊 ──────────────────────────────────────
    if action == "closeoverlay":
        # 強化版 overlay 清除 + Escape + blur；best-effort，全部 ignore error
        return out(
            line("Run Keyword And Ignore Error", "Evaluate JavaScript", "${None}", _OVERLAY_CLEANUP_JS_ENHANCED),
            line("Run Keyword And Ignore Error", "Press Keys", "${None}", "Escape"),
            line("Run Keyword And Ignore Error", "Evaluate JavaScript", "${None}",
                 "() => { try { document.activeElement?.blur(); } catch(e) {} }"),
        )

    if action == "clickoutside":
        # 點頁面 (8,8) 觸發 outside-click，收合 dropdown/popover
        click_outside_js = (
            "() => { const el = document.elementFromPoint(8,8); "
            "if (el) { el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:8,clientY:8})); } "
            "else { document.body.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); } }"
        )
        steps = []
        if locator:
            steps.append(line("Run Keyword And Ignore Error", "Wait For Elements State", locator, "visible", "timeout=10s"))
        steps.append(line("Evaluate JavaScript", "${None}", click_outside_js))
        return out(*steps)

    if action == "pressescape":
        # 發送 Escape 鍵，關閉 dialog/dropdown/custom modal
        return out(line("Press Keys", "${None}", "Escape"))

    if action == "forceclick":
        # 繞過 actionability 檢查的強制點擊（Playwright force=True）
        return out(
            _overlay_cleanup_line(),
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Click With Options", locator, "force=True"),
        )

    if action == "clickjs":
        # 用 JS dispatchEvent 繞過 pointer event 攔截
        click_js = "(el) => el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,composed:true}))"
        return out(
            _overlay_cleanup_line(),
            line("Wait For Elements State", locator, "visible", "timeout=10s"),
            line("Evaluate JavaScript", locator, click_js),
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
        return out(line("Wait For Elements State", locator, "visible", "timeout=20s"))
    if action in ("asserthidden", "shouldbehidden"):
        return out(line("Wait For Elements State", locator, "hidden", "timeout=20s"))
    if action == "assertchecked":
        return out_w(
            line("${state}=", "Get Checkbox State", locator),
            line("Should Be True", "${state}"),
        )
    if action == "assertenabled":
        return out(line("Wait For Elements State", locator, "enabled", "timeout=20s"))
    if action == "assertdisabled":
        return out(line("Wait For Elements State", locator, "disabled", "timeout=20s"))
    if action == "asserttext":
        # 文字比對預設使用 Contains（比 Equals 實用）
        return out_w(
            line("${actual}=", "Get Text", locator),
            compare_line("${actual}", compare or "Contains", expected),
        )
    if action == "assertvalue":
        return out_w(
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
        return out_w(
            line("${attr}=", "Get Attribute", locator, value or "value"),
            compare_line("${attr}", compare or "Equals", expected),
        )
    if action == "assertimageloaded":
        # 檢查 <img> 是否真的載入完成（complete=true && naturalWidth>0），
        # 避免破圖被當成「顯示」通過
        return out(
            line("Wait For Elements State", locator, "visible", "timeout=20s"),
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
            line("Wait For Elements State", locator, "visible", "timeout=20s"),
            line("${bb}=", "Get Boundingbox", locator),
            line("Should Be True", f"abs(${{bb}}[\"x\"] - {ex}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"y\"] - {ey}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"width\"] - {ew}) <= {tol}"),
            line("Should Be True", f"abs(${{bb}}[\"height\"] - {eh}) <= {tol}"),
        )

    # ── Screenshot Diff：AssertScreenshotMatch ─────────────────────
    # 要求步驟必須有穩定的 ``id`` UUID（前端建立 step 時自動產生）。
    # locator 給的話只截單一元素；空就截整頁。
    # expected 為「百分比門檻」字串，可寫 "1.5" 或 "1.5%"；空白則用預設 1.0。
    if action in ("assertscreenshotmatch", "screenshotmatch", "screenshotdiff"):
        step_uuid = (step.get("id") or "").strip()
        if not step_uuid:
            return [line("Fail", "AssertScreenshotMatch 需要 step 必須有 id（UUID）；"
                                 "請在前端編輯器重新拉一次此 step 以產生新 id")]
        thresh_raw = (expected or "").strip().rstrip("%") or "1.0"
        screenshot_filename = f"/tmp/assertshot_{step_uuid}"
        if locator:
            return out(
                line("${cur_path}=", "Take Screenshot", f"filename={screenshot_filename}",
                     f"selector={locator}"),
                line("AssertScreenshot.Match", "${cur_path}", step_uuid, thresh_raw),
            )
        else:
            return out(
                line("${cur_path}=", "Take Screenshot", f"filename={screenshot_filename}",
                     "fullPage=True"),
                line("AssertScreenshot.Match", "${cur_path}", step_uuid, thresh_raw),
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

    # ── 寫入語意明確化：Insert / Update / Delete ──
    # 與 Db.Execute 行為相同（執行任意 SQL），但語意較直觀且支援 expected=affected_rows 驗證。
    # DatabaseLibrary 的 Execute Sql String 不直接回傳 affected rows；要驗證影響筆數請另寫
    # 配套的 Db.RowCount 或 Db.AssertRowExists 步驟（一個寫入 + 一個 SELECT 驗證）。
    if raw_action in ("Db.Insert", "Db.Update", "Db.Delete"):
        sql = value or locator
        return [line("Execute Sql String", sql)]

    # ── 斷言類 ──
    if raw_action == "Db.AssertRowExists":
        # input/locator = WHERE 過濾的 SELECT；至少 1 列才 PASS
        # 例如：SELECT 1 FROM users WHERE email='a@b.com'
        return [line("Check If Exists In Database", value or locator)]
    if raw_action == "Db.AssertNoRow":
        return [line("Check If Not Exists In Database", value or locator)]
    if raw_action == "Db.AssertValue":
        # input/locator = 取單一儲存格的 SELECT（建議 LIMIT 1）；
        # expected = 期望值；compare 預設 Equals。
        # 例如：locator="SELECT name FROM users WHERE id=1 LIMIT 1"  expected="Alice"
        sql = value or locator
        return [
            line("${rows}=", "Query", sql),
            line("Should Not Be Empty", "${rows}",
                 f"AssertValue: Query 沒有回傳任何列，SQL={sql!r}"),
            line("${actual}=", "Set Variable", "${rows}[0][0]"),
            line("Log", "AssertValue actual=${actual}"),
            compare_line("${actual}", compare or "Equals", expected),
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

    # ─── Capture(自動 capture 變數;Sprint 2.1) ─────────────────────
    # step.locator    = 來源:Browser 用 css/role=,Mobile 用元素 locator,Http 後讀 ${HTTP_RESP}
    # step.input      = capture spec:
    #                     ""              / "text"    → 元素文字 (Get Text)
    #                     "attr:href"                  → 元素屬性 (Get Attribute)
    #                     "json:user.id"               → 從 ${HTTP_RESP} 取 JSON path
    #                     "value"                      → input value (Get Value)
    # step.expected   = 變數名(會被存成 Suite Variable,後續 step 可 ${name} 引用)
    if raw_action == "Capture" or action == "capture":
        # 變數名:用 expected 欄位(若沒填用 "captured");清成合法 Python 識別字
        raw_var = (step.get("expected") or step.get("var_name") or "captured").strip()
        var_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_var) or "captured"
        spec = (step.get("input") or "").strip()
        if spec.startswith("attr:"):
            attr = spec[5:].strip() or "value"
            return out(
                line(f"${{{var_name}}}=", "Get Attribute", locator, attr),
                line("Set Suite Variable", f"${{{var_name}}}"),
            )
        if spec.startswith("json:"):
            path = spec[5:].strip()
            # 從 ${HTTP_RESP}(Http.* 動作存的 dict)取 dotted path
            # 用 Evaluate 比較安全;失敗回空字串
            py_expr = (
                "(lambda d, p: __import__('functools').reduce("
                "lambda v, k: (v.get(k) if isinstance(v, dict) else (v[int(k)] if isinstance(v, list) and k.isdigit() else None))"
                ", p.split('.'), d) if isinstance(d, (dict, list)) else '')"
                f"(${{HTTP_RESP}}, '{_rf_escape(path)}')"
            )
            return out(
                line(f"${{{var_name}}}=", "Evaluate", py_expr),
                line("Set Suite Variable", f"${{{var_name}}}"),
            )
        if spec == "value":
            # input 元素的 value 屬性
            return out(
                line(f"${{{var_name}}}=", "Get Property", locator, "value"),
                line("Set Suite Variable", f"${{{var_name}}}"),
            )
        # 預設:取元素文字
        return out(
            line(f"${{{var_name}}}=", "Get Text", locator),
            line("Set Suite Variable", f"${{{var_name}}}"),
        )

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
    enable_recording: bool = True,
    video_dir: Optional[str] = None,
    trace_dir: Optional[str] = None,
    project_env_vars: Optional[dict[str, str]] = None,
    project_devices: Optional[list[dict]] = None,
) -> tuple[str, list[list[dict]]]:
    """
    回傳 (.robot 檔內容, 每個 test case 的 step 清單)。
    每個 step 在 .robot 中會被包成：
        Log    AT_STEP idx=N
        Take Screenshot    filename=...    fullPage=True
        <action keyword(s)>
        Take Screenshot    filename=...    fullPage=True

    enable_recording=True 時：
      - New Context 設定 recordVideo（產生 .webm）
      - Setup 內呼叫 New Context 之後 Start Tracing  / Teardown 前 Stop Tracing -> trace.zip
    """
    rows = (ddt or {}).get("rows") or []
    headers = (ddt or {}).get("headers") or []
    if not rows:
        rows = [[]]

    # 在 .robot 中以正斜線表示路徑（Windows 與 Linux 都吃；避免反斜線跳脫困擾）
    def _posix(p: Optional[str]) -> str:
        return (p or "").replace("\\", "/")

    video_dir_p = _posix(video_dir) if enable_recording else ""
    trace_dir_p = _posix(trace_dir) if enable_recording else ""

    # AppiumLibrary 跟 Browser Library 有同名 keyword(Get Text / Click / 等),
    # RF 的 `WITH NAME` 只改 library 別名,不會把短名稱從查找移除,所以兩者一起
    # 載入時所有 `Get Text` 之類的短呼叫都會 ambiguous。改為「只在這個案例真的
    # 有用到 Mobile.* 動作時才 import」,純 WEB / API / DB 不載入。
    has_mobile_action = any(
        (s.get("action") or "").strip().lower().startswith("mobile.")
        for s in steps
    )

    lines: list[str] = []
    lines.append("*** Settings ***")
    lines.append("Library    Browser    auto_closing_level=TEST")
    lines.append("Library    RequestsLibrary")
    lines.append("Library    DatabaseLibrary")
    if has_mobile_action:
        lines.append("Library    AppiumLibrary")
    lines.append("Library    Collections")
    lines.append("Library    OperatingSystem")
    lines.append("Library    String")
    lines.append("Library    DateTime")
    # ── Screenshot diff（自製 Python library；spawn 容器內已 COPY 進 /app/tasks/）──
    lines.append("Library    tasks.assert_screenshot_lib    WITH NAME    AssertScreenshot")
    lines.append("")
    # continue-on-failure:任一 step 失敗 RF 仍繼續跑剩下的 step。整個 test
    # 最終 status 還是 FAIL,但錄影 / trace 不會在第一個錯就終止,使用者能看
    # 到後續步驟的實際畫面。Cascade fail(前面失敗導致後面找不到元素)會出
    # 現「後段影片大多是靜止的頁面」,這是 SPA 狀態錯亂的自然結果、無法繞。
    lines.append("Test Tags    robot:continue-on-failure")
    lines.append("")
    # Http.* 動作所使用的共用 suite 變數（SetHeader / SetBaseURL / SetAuth 寫入；
    # GET/POST/... 使用；AssertStatus / ExtractJson / SaveToken 讀取 HTTP_RESP）
    lines.append("*** Variables ***")
    lines.append("&{HTTP_HEADERS}")
    lines.append("${HTTP_BASE_URL}    ${EMPTY}")
    lines.append("${HTTP_AUTH}    ${EMPTY}")
    lines.append("${HTTP_RESP}    ${None}")
    # ── 全專案環境變數 ──
    # 來自 project_env_vars 表；每筆 name → value 直接做為 suite-level scalar variable，
    # 步驟編輯器內可寫 ${BASE_URL} / ${API_TOKEN} 等被 Robot 自動展開。
    for k, v in (project_env_vars or {}).items():
        # 只允許合法 Robot 變數名稱（[A-Za-z_][A-Za-z0-9_]*）；不合法的略過
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            continue
        lines.append(f"${{{k}}}    {_rf_escape(v)}")
    # ── 全專案設備資訊 ──
    # 每個 device 注入成 ``&{DEVICE_<label>}`` 字典，包含 Appium capabilities；
    # 例：${DEVICE_pixel5.platformName} / ${DEVICE_pixel5.deviceName}
    for d in (project_devices or []):
        label = d.get("label") or ""
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", label):
            continue
        plat = (d.get("platform") or "").upper()
        # automationName 沒給就依 platform 自動帶 default
        auto_name = d.get("automation_name") or (
            "UiAutomator2" if plat == "ANDROID" else "XCUITest" if plat == "IOS" else ""
        )
        kvs: list[tuple[str, str]] = [
            ("platformName", "Android" if plat == "ANDROID" else "iOS" if plat == "IOS" else ""),
            ("platformVersion", d.get("platform_version") or ""),
            ("deviceName", d.get("device_name") or ""),
            ("automationName", auto_name),
        ]
        if plat == "ANDROID" and d.get("avd_name"):
            kvs.append(("avd", d["avd_name"]))
        if plat == "IOS" and d.get("udid"):
            kvs.append(("udid", d["udid"]))
        for ck, cv in (d.get("extra_caps_json") or {}).items():
            kvs.append((str(ck), str(cv)))
        # 過濾掉空值，避免空字串覆蓋 Appium 預設值
        kv_str = "    ".join(f"{ck}={_rf_escape(cv)}" for ck, cv in kvs if cv != "")
        if kv_str:
            lines.append(f"&{{DEVICE_{label}}}    {kv_str}")
    lines.append("")
    lines.append("*** Keywords ***")
    lines.append("Setup Browser Session")
    lines.append(f"    New Browser    chromium    headless={'true' if headless else 'false'}")
    # ── New Context 一次帶齊：viewport / recordVideo / tracing ──
    # robotframework-browser 19.x：
    #   - tracing 參數型別改為 Union[bool, Path]；給 True 即啟用，trace 會寫到
    #     ``${OUTPUT_DIR}/browser/traces_full/<random>.zip``，context 關閉時自動寫出
    #   - 我們在 worker 端不依賴特定檔名，listener.close() 會 glob outputdir 找出 trace.zip
    #     並依 test_name 對應上傳到 MinIO
    nc_args = ["viewport={'width': 1280, 'height': 720}"]
    if enable_recording and video_dir_p:
        nc_args.append(
            f"recordVideo={{'dir': '{video_dir_p}', 'size': {{'width': 1280, 'height': 720}}}}"
        )
    if enable_recording and trace_dir_p:
        nc_args.append("tracing=True")
    lines.append("    New Context    " + "    ".join(nc_args))
    lines.append("    New Page")
    # 預設所有 Browser Library 動作（Click / Fill / Wait For Elements State / ...）
    # 超過 20 秒就算失敗;之前 60s 在 cascade fail 場景下會讓每步白等 60s,
    # 58 步 = 1 小時靜止畫面。20s 對正常 SPA 元素足夠,fail 也快很多。
    lines.append("    Set Browser Timeout    20s")
    # 把錄影起始時間（epoch 秒）寫入 RECORDING_START；listener 用此計算每步的 video offset
    lines.append("    ${RECORDING_START}=    Get Time    epoch")
    lines.append("    Set Suite Variable    ${RECORDING_START}")
    lines.append("    Log    RECORDING_START ts=${RECORDING_START}")
    lines.append("")
    lines.append("Teardown Browser Session")
    # 關閉 context 會自動寫出 trace.zip 與最終化 .webm（不需顯式 Stop Tracing）
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
            # Sprint 4.1 — 條件分支邊界 step:If / ElseIf / Else / EndIf
            # 用 expected 欄位當 condition expression(例 "${count} > 5" 或 "'${status}' == 'success'")
            # 不截圖、不 translate,直接輸出 Robot 5.0+ 原生 IF/ELSE/END 語法。
            #
            # 欄位來源:優先讀 step.compare(新前端把控制流放這欄,語意更清晰);
            # 若是舊資料尚未遷移,退回 step.action。
            compare_lower = (step.get("compare") or "").strip().lower()
            flow_key = compare_lower if compare_lower in ("if", "elseif", "else", "endif") else action_lower
            if flow_key in ("if", "elseif", "else", "endif"):
                if flow_key == "if":
                    cond = _substitute(step.get("expected") or step.get("input") or "True", ctx)
                    lines.append(f"    IF    {cond}")
                elif flow_key == "elseif":
                    cond = _substitute(step.get("expected") or step.get("input") or "True", ctx)
                    lines.append(f"    ELSE IF    {cond}")
                elif flow_key == "else":
                    lines.append("    ELSE")
                elif flow_key == "endif":
                    lines.append("    END")
                continue  # 跳過後續截圖 + _translate_step
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
                    f"    Run Keyword And Ignore Error    Take Screenshot    filename={pre_path}    fullPage=True"
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
                    f"    Run Keyword And Ignore Error    Take Screenshot    filename={post_path}    fullPage=True"
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
    enable_recording: bool = True,
    project_env_vars: Optional[dict[str, str]] = None,
    project_devices: Optional[list[dict]] = None,
) -> list[CaseResult]:
    """
    執行單一測試案例（含 DDT 多列）— **Spawn 容器模式**。

    流程：
      1. 在 worker process 內產生 .robot 文字
      2. 把 .robot 上傳到 MinIO (``inputs/<task>/<case>.robot``)
      3. 透過 docker SDK spawn 一個 ``ROBOT_RUNNER_IMAGE`` 容器
         （預設 ``autotest-robot-runner:1.1.0``，base = ppodgorsek/robot-framework:7.3.1）
      4. 容器 entrypoint = ``robot_container.py``：拉 .robot → 跑 robot
         → listener 即時把截圖／影片／trace 上傳到 MinIO → 寫 step_results.json 到 MinIO
      5. worker 等容器結束、抓回 step_results.json、解析成 CaseResult

    enable_recording=True 時 listener 會啟動 Trace + Video 並把產物 URL 寫入 step_records。
    無論本地是否裝 robot，本函式都不在 worker process 內跑 robot subprocess。
    """
    if not steps:
        publish_log("ERROR", "  ⚠ 此案例 steps_json 為空")
        return [CaseResult(passed=False, steps=[], duration_ms=0)]

    if (settings.STORAGE_BACKEND or "").lower() != "s3":
        publish_log(
            "ERROR",
            f"  💥 Spawn 模式需要 STORAGE_BACKEND=s3；目前是 '{settings.STORAGE_BACKEND}'，請改 .env 後重啟",
        )
        return [CaseResult(passed=False, steps=[], duration_ms=0)]

    # ── 1) 產生 .robot 文字 ────────────────────────────
    # spawn 模式下 video_dir / trace_dir 是「容器內」的暫存路徑（由 robot_container.py 決定），
    # 不需要在 worker process 端真的建目錄。listener 會把產物存到 SeaweedFS 而非本地檔系統。
    robot_text, _ = _build_robot_file(
        steps,
        ddt,
        case_tag,
        screenshot_dir="/work/screenshots",
        headless=headless,
        enable_recording=enable_recording,
        video_dir="/work/videos" if enable_recording else None,
        trace_dir="/work/traces" if enable_recording else None,
        project_env_vars=project_env_vars,
        project_devices=project_devices,
    )

    task_id = _extract_task_id(publish_log) or report_id

    # ── 2) 上傳 .robot 到 SeaweedFS ────────────────────────
    robot_key = f"inputs/{task_id}/{case_tag}.robot"
    result_key = f"results-json/{task_id}/{case_tag}.json"
    try:
        from app.services.storage_service import save_bytes  # type: ignore

        save_bytes(robot_text.encode("utf-8"), robot_key, bucket="results", content_type="text/plain")
        publish_log("INFO", f"  ⬆ .robot 上傳至 SeaweedFS key={robot_key}")
    except Exception as e:
        publish_log("ERROR", f"  💥 .robot 上傳 SeaweedFS 失敗: {e}")
        return [CaseResult(passed=False, steps=[], duration_ms=0)]

    # ── 3) Spawn 容器 ─────────────────────────────────
    image = os.environ.get("ROBOT_RUNNER_IMAGE", "autotest-robot-runner:1.1.0")
    network = os.environ.get("ROBOT_RUNNER_NETWORK", "autotest_default")
    # 容器執行上限,預設 30 分鐘;可由 env var 覆寫。
    # 容器內 robot subprocess 預留 120s 緩衝給寫 result JSON + S3 上傳。
    runner_timeout_sec = int(os.environ.get("RUNNER_CONTAINER_TIMEOUT_SEC", "1800"))
    robot_subprocess_timeout = max(60, runner_timeout_sec - 120)
    container_env = {
        "JOB_TASK_ID": task_id,
        "JOB_REPORT_ID": report_id,
        "JOB_CASE_TAG": case_tag,
        "JOB_ROBOT_KEY": robot_key,
        "JOB_RESULT_KEY": result_key,
        "PLAYWRIGHT_HEADLESS": "1" if headless else "0",
        "STORAGE_BACKEND": "s3",
        "S3_ENDPOINT": settings.S3_ENDPOINT,
        "S3_ACCESS_KEY": settings.S3_ACCESS_KEY,
        "S3_SECRET_KEY": settings.S3_SECRET_KEY,
        "REDIS_URL": settings.REDIS_URL,
        "BASE_URL": settings.BASE_URL,
        "AUTOTEST_TASK_ID": task_id,
        "AUTOTEST_REPORT_ID": report_id,
        "ENABLE_RECORDING": "1" if enable_recording else "0",
        # robot subprocess 的逾時設定(由 robot_container.py 讀取)
        "ROBOT_SUBPROCESS_TIMEOUT_SEC": str(robot_subprocess_timeout),
    }

    publish_log("INFO", f"  🐳 啟動容器 image={image} (network={network})")
    case_start = time.time()
    rc = -1
    timed_out = False
    try:
        import docker  # type: ignore

        client = docker.from_env()
        container = client.containers.run(
            image=image,
            environment=container_env,
            network=network,
            detach=True,
            remove=False,  # auto_remove=True 會搶在 wait() 之前把容器砍掉拿不到 exit code
            name=f"robot-{task_id[:8]}-{case_tag[:8]}",
        )
    except Exception as e:
        publish_log("ERROR", f"  💥 docker run 失敗: {e}")
        return [CaseResult(
            passed=False,
            steps=[StepResult(
                status="FAILED",
                duration_ms=0,
                error_message=f"Runner container 啟動失敗: {e}",
                pre_screenshot_url=None,
                post_screenshot_url=None,
                target_highlight_json=None,
            )],
            duration_ms=0,
        )]

    # 用短輪詢取代 container.wait() long-poll:
    # docker-socket-proxy (haproxy) 預設 server timeout 10m 會切斷長連線,造成我們的
    # runner_timeout_sec(1800s 或更長)實際打不到 — 容器在 600s 就被誤判逾時。
    # 改成每 2 秒 reload 一次容器狀態,每次都是短 HTTP,proxy idle 永遠不會觸發。
    poll_interval = 2.0
    poll_start = time.time()
    try:
        while True:
            try:
                container.reload()
            except Exception as e:
                # proxy / docker daemon 連線瞬斷:再試一次,別直接判逾時
                publish_log("WARN", f"  ⚠ docker reload 失敗,重試: {e}")
                time.sleep(poll_interval)
                continue
            state = (container.attrs or {}).get("State") or {}
            status = state.get("Status") or ""
            if status in ("exited", "dead"):
                rc = int(state.get("ExitCode", -1) or -1)
                break
            if time.time() - poll_start > runner_timeout_sec:
                publish_log(
                    "ERROR",
                    f"  ⏱ 容器逾時(>{runner_timeout_sec}s),強制中止",
                )
                timed_out = True
                try:
                    container.kill()
                except Exception:
                    pass
                break
            time.sleep(poll_interval)
    finally:
        # 印出容器最後 30 行 stdout/stderr 方便除錯
        try:
            tail = container.logs(tail=30).decode("utf-8", errors="replace")
            for line in tail.splitlines()[-30:]:
                publish_log("INFO", f"  ┃ {line}")
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass

    case_dur = int((time.time() - case_start) * 1000)
    publish_log("INFO", f"  🏁 容器結束 rc={rc} ({case_dur}ms)")

    # ── 5) 從 MinIO 抓 step_results.json ───────────────
    step_records: list[dict] = []
    try:
        import boto3  # type: ignore

        s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1",
        )
        obj = s3.get_object(Bucket="results", Key=result_key)
        step_records = json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        publish_log("ERROR", f"  💥 下載 step_results.json 失敗: {e}")

    if not step_records:
        # 沒拿到 step 結果 — 通常是「容器逾時被砍」或「robot listener 沒寫完 JSON」。
        # 寫一條 synthetic FAILED step 進去,免得 report 看起來完全空白、讓使用者
        # 找不到失敗原因。
        if timed_out:
            elapsed_sec = case_dur // 1000
            msg = (
                f"Runner 容器在 {elapsed_sec}s 被中止(我端設定 timeout="
                f"{runner_timeout_sec}s,但 docker-socket-proxy 預設 server "
                f"timeout 600s 會更早切斷 long-polling)。可能原因:"
                f"(1) Goto 的目標頁卡在某個 XHR 載入(預設 wait_until=load,SPA "
                f"常因內部 API 不通而 hang;新版改用 wait_until=domcontentloaded "
                f"+ 30s timeout 已修);"
                f"(2) 測試步驟太多 + 元素 60s wait 累積;"
                f"(3) 頁面 / 元素卡 loading。"
            )
        else:
            msg = (
                f"Runner 容器 rc={rc} 但沒產出 step_results.json — "
                f"通常表示 robot listener 在寫入前 crash 或 S3 上傳失敗。"
                f"檢查容器最後 30 行 log(上面 INFO ┃ 開頭)。"
            )
        return [CaseResult(
            passed=False,
            steps=[StepResult(
                status="FAILED",
                duration_ms=case_dur,
                error_message=msg,
                pre_screenshot_url=None,
                post_screenshot_url=None,
                target_highlight_json=None,
            )],
            duration_ms=case_dur,
        )]

    # ── 6) 解析成 CaseResult（保留原本邏輯，但所有 URL 都來自 step_records）──
    rows = (ddt or {}).get("rows") or []
    n_rows = max(1, len(rows))
    n_steps = len(steps)

    case_results: list[CaseResult] = []
    for row_i in range(n_rows):
        row_steps: list[StepResult] = []
        row_passed = True
        row_dur = 0
        # 同一 row 的所有 step 都共享 case 級 trace_url / video_url（取第一個有值的）
        case_trace_url: Optional[str] = None
        case_video_url: Optional[str] = None
        for step_i in range(n_steps):
            global_idx = row_i * 1000 + step_i
            rec = next((r for r in step_records if r.get("step_index") == global_idx), None)
            if rec is None:
                row_steps.append(
                    StepResult(
                        status="SKIPPED",
                        duration_ms=0,
                        error_message=None,
                        pre_screenshot_url=None,
                        post_screenshot_url=None,
                        target_highlight_json=None,
                    )
                )
                continue
            status = rec.get("status", "FAILED")
            row_dur += rec.get("duration_ms", 0)
            if status != "PASSED":
                row_passed = False

            # 案例級 URL 由容器寫入 first-step record；若該欄位有值就 propagate 給整 row
            if rec.get("trace_url") and not case_trace_url:
                case_trace_url = rec["trace_url"]
            if rec.get("video_url") and not case_video_url:
                case_video_url = rec["video_url"]

            row_steps.append(
                StepResult(
                    status=status,
                    duration_ms=rec.get("duration_ms", 0),
                    error_message=rec.get("error"),
                    pre_screenshot_url=rec.get("pre"),
                    post_screenshot_url=rec.get("post"),
                    target_highlight_json=None,
                    step_video_url=rec.get("step_video_url"),
                    screenshot_baseline_url=rec.get("screenshot_baseline_url"),
                    screenshot_diff_url=rec.get("screenshot_diff_url"),
                    screenshot_diff_pct=rec.get("screenshot_diff_pct"),
                )
            )
        case_results.append(
            CaseResult(
                passed=row_passed,
                steps=row_steps,
                duration_ms=row_dur,
                trace_url=case_trace_url,
                video_url=case_video_url,
            )
        )

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
        if (settings.STORAGE_BACKEND or "").lower() == "s3":
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


# ════════════════════════════════════════════════════════════════
# Trace / Video 後處理
# ════════════════════════════════════════════════════════════════


def _collect_traces(
    trace_dir: str, output_dir: str, n_rows: int, case_tag: str
) -> dict[str, str]:
    """
    回傳 test_name → trace.zip 絕對路徑（已搬到 ``trace_dir`` 下）。

    Browser Library 18.x 把 trace 寫到 Robot ``--outputdir`` 之下、檔名為 New Context
    的 ``tracing=`` 參數。我們在 Setup 用 ``tracing=${TEST_NAME}.zip``，因此原始檔位於
    ``<output_dir>/<test_name>.zip``。為了能透過 ``/pics/...`` 對外服務，這裡將檔案搬
    到 PIC_FOLDER 之下的 ``trace_dir``（與截圖、錄影同一個 report 目錄）。
    """
    out: dict[str, str] = {}
    if not os.path.isdir(trace_dir) or not os.path.isdir(output_dir):
        return out
    for row_i in range(n_rows):
        test_name = f"{case_tag}_row{row_i:02d}"
        src = os.path.join(output_dir, f"{test_name}.zip")
        if not os.path.isfile(src):
            continue
        dst = os.path.join(trace_dir, f"{test_name}.zip")
        try:
            if os.path.isfile(dst):
                os.remove(dst)
            shutil.move(src, dst)
            out[test_name] = dst
        except OSError:
            # 移動失敗就直接讀原位（仍會在 _safe_cleanup 時被刪掉，但至少這次能登錄到 DB）
            out[test_name] = src
    return out


def _collect_videos(video_dir: str, n_rows: int, case_tag: str) -> dict[str, str]:
    """
    回傳 test_name → 完整錄影 .webm 絕對路徑。

    Playwright 給 video 的檔名是隨機 hash，無法由名稱直接對應到 test_name。
    由於 .robot 內 DDT row 是依序執行（列 0 → 列 N-1），且每個 test 結束時才完成 .webm 寫檔，
    我們以「修改時間排序」對應到 row index。
    """
    out: dict[str, str] = {}
    if not os.path.isdir(video_dir):
        return out
    candidates = [
        os.path.join(video_dir, fn)
        for fn in os.listdir(video_dir)
        # 只取 Playwright 寫出的「原檔」（隨機 hash 名）。排除：
        #   - 步驟切片：*_sNN.webm
        #   - 我們搬移過的成品（不論哪個 testcase）：*_rowNN.webm
        if fn.lower().endswith(".webm")
        and not re.search(r"_s\d{2}\.webm$", fn, re.IGNORECASE)
        and not re.search(r"_row\d{2}\.webm$", fn, re.IGNORECASE)
    ]
    candidates.sort(key=lambda p: os.path.getmtime(p))
    for row_i in range(min(n_rows, len(candidates))):
        test_name = f"{case_tag}_row{row_i:02d}"
        # 改名為可預期的形式：<case_tag>_row<NN>.webm（同目錄移動）
        target = os.path.join(video_dir, f"{test_name}.webm")
        try:
            if os.path.abspath(candidates[row_i]) != os.path.abspath(target):
                # 移動失敗（rename 跨檔案系統等）就退回原檔
                if os.path.isfile(target):
                    os.remove(target)
                shutil.move(candidates[row_i], target)
            out[test_name] = target
        except OSError:
            out[test_name] = candidates[row_i]
    return out


_FFMPEG_OK: Optional[bool] = None


def _ffmpeg_available() -> bool:
    """
    結果快取：只探測一次（多 step 重複呼叫時不再每次 spawn）。
    回傳 False 時 caller 應跳過 step-level 切片，但仍會保留完整錄影。
    """
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        try:
            r = subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, timeout=5
            )
            _FFMPEG_OK = r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            _FFMPEG_OK = False
    return _FFMPEG_OK


def _ffmpeg_clip(
    src_path: str, out_path: str, start_sec: float, duration_sec: float
) -> Optional[str]:
    """
    用 ffmpeg 把 src_path 的 [start, start+duration] 區段切到 out_path。
    使用 ``-c copy`` 不重編，速度快但起點可能對齊到最近的 keyframe（誤差 < 1s）。
    成功回傳 out_path；失敗或 ffmpeg 不可用回傳 None。
    """
    if not _ffmpeg_available():
        return None
    if not os.path.isfile(src_path):
        return None
    if duration_sec <= 0:
        return None
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, start_sec):.3f}",
            "-i", src_path,
            "-t", f"{duration_sec:.3f}",
            "-c", "copy",
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None
