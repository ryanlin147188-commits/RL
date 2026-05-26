#!/usr/bin/env python3
"""
AutoTest 本機執行 Agent
========================

這個 script 會持續輪詢 AutoTest backend，認領「本機」模式的測試任務，
在本機瀏覽器（Playwright）內執行後把結果回報給 backend。

安裝依賴（一次性）：
    pip install playwright requests
    playwright install chromium

使用：
    python local_agent.py --server http://<你的 AutoTest 伺服器 IP>

預設 server = http://localhost。

行為：
    - 每 POLL_INTERVAL 秒輪詢 POST /api/local-runner/claim 一次
    - 有任務就開啟 Playwright browser 執行每個 testcase 的 steps
    - 執行結果（passed / failed / duration）回 POST /api/local-runner/tasks/{task_id}/complete

支援的 Action（對應 backend runner）：
    Navigate / Goto / Reload / GoBack / GoForward
    Click / DoubleClick / RightClick / ClickAt / CanvasClick
    Hover / Focus / Fill / Type / Clear / Press / Select / Check / Uncheck
    Upload / Download / DragAndDrop
    Scroll / ScrollToElement
    Wait / WaitForSelector / WaitForLoadState
    Screenshot / ExecuteScript / SwitchTab / CloseTab
    AssertText / AssertValue / AssertVisible / AssertHidden
    AssertChecked / AssertEnabled / AssertDisabled
    AssertURL / AssertTitle / AssertCount / AssertAttribute
    AssertImageLoaded / AssertBoundingBox

已知限制：
    - API（Http.*）與 Mobile.* 不支援本機 agent；請切回 Docker 模式
    - SwitchTab 在本機 agent 上只會提示新分頁已開；若測試需跨多分頁請用 Docker
    - Upload / Download 的檔案路徑是本機 agent 執行目錄下的路徑
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import traceback
import uuid
from typing import Any

try:
    import requests
except ImportError:
    print("[FATAL] 需要 requests 套件：pip install requests")
    sys.exit(1)

try:
    from playwright.sync_api import Page, TimeoutError as PWTimeout, expect, sync_playwright
except ImportError:
    print("[FATAL] 需要 playwright 套件：pip install playwright && playwright install chromium")
    sys.exit(1)


POLL_INTERVAL = 5.0  # 秒
DEFAULT_TIMEOUT_MS = 10_000  # 單一動作預設 10 秒
DEFAULT_PAGE_TIMEOUT_MS = 30_000  # 頁面層動作預設 30 秒


def log(msg: str, kind: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{kind}] {msg}", flush=True)


def substitute_ddt(value: str | None, ctx: dict[str, str]) -> str:
    """簡易 ${var} 替換：DDT headers=['No','檔案名稱','Key','Value'] 時，
    取 Key→Value 組成 ctx。若 ctx 有該 key 就替換，不然原樣留著。"""
    if not value:
        return value or ""
    out = value
    for k, v in ctx.items():
        out = out.replace("${" + k + "}", str(v))
    return out


def build_ddt_context(ddt: dict[str, Any] | None) -> list[dict[str, str]]:
    """把 ddt 轉成多列 context：每列就是一組 {Key: Value}（新版 DDT 為 No / 檔案名稱 / Key / Value）。
    回傳 list；若 DDT 沒有資料列則回傳 [{}] 代表跑一次空 context。"""
    if not ddt:
        return [{}]
    headers = [str(h) for h in (ddt.get("headers") or [])]
    rows = ddt.get("rows") or []
    try:
        key_idx = headers.index("Key")
        val_idx = headers.index("Value")
    except ValueError:
        # 舊版可能是任意 header；退化成每列一個空 ctx
        return [{}] if not rows else [{} for _ in rows]
    out: list[dict[str, str]] = []
    for row in rows:
        ctx: dict[str, str] = {}
        if key_idx < len(row) and val_idx < len(row):
            k = str(row[key_idx] or "").strip()
            v = str(row[val_idx] or "")
            if k:
                ctx[k] = v
        out.append(ctx)
    return out or [{}]


def pw_locator(page: Page, raw_locator: str):
    """把 runner 用的 locator 字串轉成 Playwright locator。

    支援：
      - 'text=XYZ'     → page.get_by_text
      - 'role=button[name="Login"]' → page.get_by_role
      - 'label=電子郵件' → page.get_by_label
      - 'css=...' / 其他 → 直接當 CSS / XPath 吃
    """
    loc = (raw_locator or "").strip()
    if loc.startswith("text="):
        return page.get_by_text(loc[5:], exact=False)
    if loc.startswith("label="):
        return page.get_by_label(loc[6:])
    if loc.startswith("role="):
        import re

        m = re.match(r'role=([A-Za-z0-9_-]+)(?:\[(.*)\])?$', loc)
        if m:
            role = m.group(1)
            attrs = m.group(2) or ""
            name = None
            nm = re.search(r'name="([^"]+)"', attrs)
            if nm:
                name = nm.group(1)
            if name:
                return page.get_by_role(role, name=name)
            return page.get_by_role(role)
    if loc.startswith("css="):
        return page.locator(loc[4:])
    if loc.startswith("xpath="):
        return page.locator(loc)
    # 預設：直接當 CSS selector 或 Playwright 通用 selector
    return page.locator(loc)


def run_step(page: Page, step: dict[str, Any], ctx: dict[str, str]) -> tuple[bool, str | None]:
    """執行單一步驟；回傳 (success, error_message)。"""
    action = (step.get("action") or "").strip().lower()
    locator_raw = substitute_ddt(step.get("loc") or step.get("locator") or "", ctx)
    value = substitute_ddt(step.get("input") or "", ctx)
    expected = substitute_ddt(step.get("expected") or "", ctx)

    try:
        # 導航
        if action in ("navigate", "goto", "open"):
            page.goto(value or locator_raw, timeout=DEFAULT_PAGE_TIMEOUT_MS)
            return True, None
        if action == "reload":
            page.reload(timeout=DEFAULT_PAGE_TIMEOUT_MS)
            return True, None
        if action == "goback":
            page.go_back()
            return True, None
        if action == "goforward":
            page.go_forward()
            return True, None

        # 點擊 / 輸入（所有 click 前等待 visible）
        if action == "click":
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            pw_locator(page, locator_raw).click(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action in ("doubleclick", "dblclick"):
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            pw_locator(page, locator_raw).dblclick(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "rightclick":
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            pw_locator(page, locator_raw).click(button="right", timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action in ("fill", "input"):
            pw_locator(page, locator_raw).fill(value, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "type":
            pw_locator(page, locator_raw).type(value, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "clear":
            pw_locator(page, locator_raw).fill("", timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "press":
            pw_locator(page, locator_raw).press(value or "Enter", timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "hover":
            pw_locator(page, locator_raw).hover(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "focus":
            pw_locator(page, locator_raw).focus(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "check":
            pw_locator(page, locator_raw).check(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "uncheck":
            pw_locator(page, locator_raw).uncheck(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "select":
            pw_locator(page, locator_raw).select_option(value, timeout=DEFAULT_TIMEOUT_MS)
            return True, None

        # 座標點擊（Canvas / 地圖 / 自訂繪圖）：value = "x,y"
        if action in ("clickat", "canvasclick"):
            import re as _re
            parts = [p for p in _re.split(r"[,\s]+", (value or "").strip()) if p]
            x, y = (parts + ["0", "0"])[:2]
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            pw_locator(page, locator_raw).click(
                position={"x": float(x), "y": float(y)}, timeout=DEFAULT_TIMEOUT_MS
            )
            return True, None

        # 拖拉：locator = 來源；value = 目標 selector
        if action == "draganddrop":
            dst = pw_locator(page, value)
            pw_locator(page, locator_raw).drag_to(dst, timeout=DEFAULT_TIMEOUT_MS)
            return True, None

        # 檔案上傳 / 下載
        if action == "upload":
            # value = 檔案路徑（本機 agent 執行時可讀）
            pw_locator(page, locator_raw).set_input_files(value)
            return True, None
        if action == "download":
            # locator = 觸發下載的連結/按鈕；value = 要儲存到的本機路徑
            save_path = value or "download"
            with page.expect_download(timeout=DEFAULT_PAGE_TIMEOUT_MS) as dl_info:
                pw_locator(page, locator_raw).click(timeout=DEFAULT_TIMEOUT_MS)
            dl_info.value.save_as(save_path)
            return True, None

        # 分頁
        if action == "switchtab":
            pages = page.context.pages
            target = (value or "NEW").strip().upper()
            if target == "NEW" and len(pages) >= 2:
                # 換到最後一個開啟的分頁
                new_page = pages[-1]
                new_page.bring_to_front()
                # 把當前 page 替換是不可能（函式參數傳值），但 Playwright context.pages 共享
                # 所以我們直接把後續動作改對應到 context 的最後一頁 → 透過 globals 很難乾淨處理
                # 這裡退而求其次：只印出提示，告知使用者在 agent 層目前只能一個 page 執行
                log(f"(SwitchTab) 已偵測到新分頁，但本機 agent 目前只跟原 page 互動；若需多分頁請改走 Docker 模式", "WARN")
            return True, None
        if action == "closetab":
            page.close()
            return True, None

        # 捲動
        if action in ("scroll",):
            import re as _re
            parts = [p for p in _re.split(r"[,\s]+", (value or "0 0").strip()) if p]
            dx, dy = (parts + ["0", "0"])[:2]
            page.mouse.wheel(float(dx), float(dy))
            return True, None
        if action == "scrolltoelement":
            pw_locator(page, locator_raw).scroll_into_view_if_needed(timeout=DEFAULT_TIMEOUT_MS)
            return True, None

        # 等待
        if action in ("wait", "sleep"):
            ms = float(value or expected or 1000)
            time.sleep(ms / 1000.0)
            return True, None
        if action in ("waitforselector", "waitfor"):
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "waitforloadstate":
            page.wait_for_load_state(value or "load", timeout=DEFAULT_PAGE_TIMEOUT_MS)
            return True, None

        # 截圖 / JS
        if action == "screenshot":
            page.screenshot(path=f"screenshot_{int(time.time())}.png")
            return True, None
        if action == "executescript":
            page.evaluate(value)
            return True, None

        # 斷言
        if action in ("assertvisible", "shouldbevisible"):
            expect(pw_locator(page, locator_raw)).to_be_visible(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action in ("asserthidden", "shouldbehidden"):
            expect(pw_locator(page, locator_raw)).to_be_hidden(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "assertchecked":
            expect(pw_locator(page, locator_raw)).to_be_checked(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "assertenabled":
            expect(pw_locator(page, locator_raw)).to_be_enabled(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "assertdisabled":
            expect(pw_locator(page, locator_raw)).to_be_disabled(timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "asserttext":
            expect(pw_locator(page, locator_raw)).to_contain_text(expected, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "assertvalue":
            expect(pw_locator(page, locator_raw)).to_have_value(expected, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "asserturl":
            expect(page).to_have_url(expected, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "asserttitle":
            expect(page).to_have_title(expected, timeout=DEFAULT_TIMEOUT_MS)
            return True, None
        if action == "assertcount":
            count = pw_locator(page, locator_raw).count()
            want = int(expected or "1")
            if count != want:
                return False, f"element count {count} != expected {want}"
            return True, None
        if action == "assertattribute":
            # value = 屬性名；expected = 期望值
            attr = pw_locator(page, locator_raw).get_attribute(value or "value")
            if str(attr or "") != str(expected or ""):
                return False, f"attribute {value!r} = {attr!r}, expected {expected!r}"
            return True, None
        if action == "assertimageloaded":
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            ok = pw_locator(page, locator_raw).evaluate(
                "(el) => el && el.complete && el.naturalWidth > 0"
            )
            if not ok:
                return False, "image not loaded (complete=false or naturalWidth=0)"
            return True, None
        if action == "assertboundingbox":
            # value = "x,y,w,h"；expected = 誤差像素（預設 2）
            import re as _re
            parts = [p for p in _re.split(r"[,\s]+", (value or "").strip()) if p]
            if len(parts) < 4:
                return False, f"AssertBoundingBox 需 value='x,y,w,h'，收到 {value!r}"
            ex, ey, ew, eh = [float(v) for v in parts[:4]]
            tol = float(expected or "2")
            pw_locator(page, locator_raw).wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            bb = pw_locator(page, locator_raw).bounding_box()
            if bb is None:
                return False, "bounding_box() returned None"
            diffs = [
                abs(bb["x"] - ex),
                abs(bb["y"] - ey),
                abs(bb["width"] - ew),
                abs(bb["height"] - eh),
            ]
            if any(d > tol for d in diffs):
                return False, (
                    f"bounding box mismatch: got {bb}, expected ({ex},{ey},{ew},{eh}) ±{tol}"
                )
            return True, None

        # 未支援的動作直接記為失敗，讓使用者知道
        return False, f"本機 agent 尚未支援動作：{step.get('action')}（建議切回 Docker 模式）"

    except PWTimeout as exc:
        return False, f"timeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def upload_screenshot_bytes(server: str, report_id: str, filename: str, img_bytes: bytes) -> Optional[str]:
    """把 PNG bytes 上傳到 backend 的 /api/local-runner/upload-screenshot。

    成功回傳 backend 提供的 URL（可存進 pre/post_screenshot_url）；失敗回傳 None。
    """
    try:
        resp = requests.post(
            f"{server}/api/local-runner/upload-screenshot",
            data={"report_id": report_id, "filename": filename},
            files={"file": (filename, img_bytes, "image/png")},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("url")
    except Exception as exc:  # noqa: BLE001
        log(f"上傳截圖失敗（{filename}）：{exc}", "WARN")
        return None


def safe_screenshot(page: Page) -> Optional[bytes]:
    """嘗試 page.screenshot()；失敗時吞錯回 None（例如 page 已關）。"""
    try:
        return page.screenshot(full_page=False, type="png", timeout=5000)
    except Exception as exc:  # noqa: BLE001
        log(f"截圖失敗：{exc}", "WARN")
        return None


# 這些 action 本質上沒有單一目標元素，不用也不能畫紅框
_ACTIONS_WITHOUT_TARGET = {
    "navigate", "goto", "open", "reload", "goback", "goforward",
    "wait", "sleep", "waitforloadstate",
    "screenshot", "executescript", "switchtab", "closetab", "scroll",
}


def highlight_element(page: Page, step: dict[str, Any], ctx: dict[str, str]) -> bool:
    """在目標元素周圍畫紅框（fixed overlay div）。

    成功回 True。此函式設計成「盡力而為」：找不到元素、timeout 都不會拋例外，
    只是沒畫框，後續截圖照樣能拍。
    """
    action = (step.get("action") or "").strip().lower()
    if action in _ACTIONS_WITHOUT_TARGET:
        return False
    locator_raw = substitute_ddt(step.get("loc") or step.get("locator") or "", ctx)
    if not locator_raw.strip():
        return False
    try:
        bb = pw_locator(page, locator_raw).first.bounding_box(timeout=2000)
    except Exception:  # noqa: BLE001
        return False
    if not bb:
        return False
    try:
        page.evaluate(
            """(bb) => {
                const o = document.createElement('div');
                o.className = '__autotest_hl';
                Object.assign(o.style, {
                    position: 'fixed',
                    top: (bb.y - 2) + 'px',
                    left: (bb.x - 2) + 'px',
                    width: (bb.width + 4) + 'px',
                    height: (bb.height + 4) + 'px',
                    border: '3px solid red',
                    boxSizing: 'border-box',
                    pointerEvents: 'none',
                    zIndex: '2147483647',
                    boxShadow: '0 0 0 1px rgba(255,255,255,0.8) inset',
                });
                document.body.appendChild(o);
            }""",
            {"x": bb["x"], "y": bb["y"], "width": bb["width"], "height": bb["height"]},
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def remove_highlights(page: Page) -> None:
    """清除所有 autotest 畫的紅框 overlay。"""
    try:
        page.evaluate(
            "document.querySelectorAll('.__autotest_hl').forEach(e => e.remove())"
        )
    except Exception:  # noqa: BLE001
        pass


def run_case(
    page: Page,
    case: dict[str, Any],
    server: str,
    report_id: str,
    project_env_vars: dict[str, str] | None = None,
) -> tuple[bool, int, list[dict[str, Any]]]:
    """執行一整個 testcase。

    回傳:(全部 passed?, 失敗步驟數, 每步記錄 list)
    每步記錄欄位與 ExecutionStepLog 對齊:
        testcase_node_id / step_index / status / duration_ms / error_message
        + pre_screenshot_url / post_screenshot_url(本機 agent 上傳後拿到的 URL)
    step_index 沿用 Docker runner 的編碼:round_idx * 1000 + step_position

    `project_env_vars`:專案環境變數,跟 DDT row 同名時 DDT row 優先(跟
    docker runner 規則一致)。
    """
    steps = case.get("steps_json") or []
    ddt_contexts = build_ddt_context(case.get("ddt_json"))
    env_map = project_env_vars or {}
    all_passed = True
    fail_count = 0
    step_logs: list[dict[str, Any]] = []
    tc_id = case.get("testcase_id") or "case"
    tc_short = (tc_id or "case")[:8]
    for round_i, ddt_ctx in enumerate(ddt_contexts):
        # env 是底,DDT row 蓋上去(同名 key 由 DDT 定義者覆蓋專案層)
        ctx = {**env_map, **ddt_ctx}
        log(f"  ─ Round {round_i + 1} / {len(ddt_contexts)} (ctx={list(ctx.keys()) or 'none'})")
        for i, step in enumerate(steps):
            desc = step.get("desc") or step.get("action") or f"step {i + 1}"
            # 執行前：畫紅框 → 截圖 → 清除紅框（和 Docker runner 的行為一致）
            highlight_element(page, step, ctx)
            pre_bytes = safe_screenshot(page)
            remove_highlights(page)
            pre_fn = f"tc_{tc_short}_row{round_i:02d}_s{i:02d}_pre.png"
            pre_url = upload_screenshot_bytes(server, report_id, pre_fn, pre_bytes) if pre_bytes else None

            t0 = time.time()
            ok, err = run_step(page, step, ctx)
            dur_ms = int((time.time() - t0) * 1000)

            # 執行後截圖（不畫框，顯示動作後的實際狀態）→ 上傳
            post_bytes = safe_screenshot(page)
            post_fn = f"tc_{tc_short}_row{round_i:02d}_s{i:02d}_post.png"
            post_url = upload_screenshot_bytes(server, report_id, post_fn, post_bytes) if post_bytes else None

            step_logs.append({
                "testcase_node_id": tc_id,
                "step_index": round_i * 1000 + i,
                "status": "PASSED" if ok else "FAILED",
                "duration_ms": dur_ms,
                "error_message": err,
                "pre_screenshot_url": pre_url,
                "post_screenshot_url": post_url,
            })
            if ok:
                log(f"    ✓ [{i + 1}] {desc} ({dur_ms}ms)")
            else:
                all_passed = False
                fail_count += 1
                log(f"    ✗ [{i + 1}] {desc} → {err} ({dur_ms}ms)", "ERROR")
                # 維持 robot:continue-on-failure 的語意：失敗了仍繼續跑後面步驟
    return all_passed, fail_count, step_logs


def process_job(job: dict[str, Any], server: str) -> None:
    report_id = job.get("report_id")
    task_id = job.get("task_id")
    cases = job.get("cases") or []
    project_env_vars = job.get("project_env_vars") or {}
    setup_ids = set(job.get("setup_testcase_ids") or [])
    log(
        f"🚀 接到任務 task={task_id[:8]}…,共 {len(cases)} 個案例 "
        f"(setup={len(setup_ids)},env={len(project_env_vars)})"
    )

    passed = 0
    failed = 0
    setup_failed = False
    start_ts = time.time()
    all_step_logs: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            for idx, case in enumerate(cases, 1):
                tc_id = case.get("testcase_id") or ""
                is_setup = bool(case.get("is_setup")) or (tc_id in setup_ids)
                phase_label = "🔧 SETUP" if is_setup else "▶"
                log(f"{phase_label} [{idx}/{len(cases)}] 案例 {tc_id[:8]}…")

                if setup_failed and not is_setup:
                    log(f"⏭ 前置案例已失敗,跳過主案例 {tc_id[:8]}…", "WARN")
                    failed += 1
                    all_step_logs.append({
                        "testcase_node_id": tc_id,
                        "step_index": 0,
                        "status": "FAILED",
                        "duration_ms": 0,
                        "error_message": "Skipped: precondition testcase failed",
                    })
                    continue

                try:
                    ok, fc, logs = run_case(
                        page, case, server, report_id,
                        project_env_vars=project_env_vars,
                    )
                    all_step_logs.extend(logs)
                    if ok:
                        passed += 1
                        log(f"✅ 案例 {idx} 通過")
                    else:
                        failed += 1
                        log(f"❌ 案例 {idx} 失敗({fc} 步)")
                        if is_setup:
                            setup_failed = True
                            log(f"🛑 前置案例 {tc_id[:8]} 失敗,後續主案例跳過", "ERROR")
                except Exception as exc:  # 單一 case 異常不整體中止  # noqa: BLE001
                    failed += 1
                    log(f"💥 案例 {idx} 執行器例外:{exc}", "ERROR")
                    traceback.print_exc()
                    # 也寫一筆失敗 step log 標記該案例出事
                    all_step_logs.append({
                        "testcase_node_id": tc_id,
                        "step_index": 0,
                        "status": "FAILED",
                        "duration_ms": 0,
                        "error_message": f"Runner exception: {exc}",
                    })
                    if is_setup:
                        setup_failed = True
                        log(f"🛑 前置案例 {tc_id[:8]} 例外,後續主案例跳過", "ERROR")
        finally:
            try:
                context.close()
            finally:
                browser.close()

    dur_ms = int((time.time() - start_ts) * 1000)
    status = "PASSED" if failed == 0 else "FAILED"
    log(f"🏁 完成：passed={passed} failed={failed} duration={dur_ms}ms → {status}")

    # 回報
    try:
        r = requests.post(
            f"{server}/api/local-runner/tasks/{task_id}/complete",
            json={
                "status": status,
                "passed_cases": passed,
                "failed_cases": failed,
                "duration_ms": dur_ms,
                "steps": all_step_logs,
            },
            timeout=60,
        )
        r.raise_for_status()
        log(f"📤 已回報 backend（report_id={report_id}，共 {len(all_step_logs)} 步）")
    except Exception as exc:  # noqa: BLE001
        log(f"回報失敗：{exc}", "ERROR")


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoTest 本機執行 Agent")
    parser.add_argument("--server", default="http://localhost", help="AutoTest backend base URL，預設 http://localhost")
    parser.add_argument("--agent-id", default=f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}", help="Agent 識別字串")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL, help="輪詢間隔秒數，預設 5")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    log(f"Agent 啟動 (agent_id={args.agent_id})，server={server}，poll={args.interval}s")
    log("按 Ctrl+C 停止")

    while True:
        try:
            resp = requests.post(
                f"{server}/api/local-runner/claim",
                json={"agent_id": args.agent_id},
                timeout=15,
            )
            if resp.status_code == 204:
                time.sleep(args.interval)
                continue
            if resp.status_code >= 400:
                log(f"claim 失敗 HTTP {resp.status_code}：{resp.text[:200]}", "ERROR")
                time.sleep(args.interval)
                continue
            job = resp.json()
            process_job(job, server)
        except KeyboardInterrupt:
            log("使用者中斷，離開")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"主迴圈錯誤：{exc}", "ERROR")
            traceback.print_exc()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
