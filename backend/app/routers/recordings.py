"""
錄製功能 REST API。

工作流：
1. 前端 POST /api/recordings 建立 session，回傳 session_id 與三組可選指令：
   - npx playwright codegen ...      （只需 Node.js）
   - python -m playwright codegen ...（需 pip install playwright）
   - rfbrowser codegen ...           （robotframework-browser，含於 celery image）
2. 使用者於本機執行任一指令，會開啟 Playwright 視窗錄製，
   結束後產生 recorded.py + trace.zip。
3. 前端透過 POST /api/recordings/{id}/upload 把兩個檔案上傳。
4. 後端解析 .py 內 page.goto / click / fill / press / check 等呼叫，
   POST /convert 回傳 BDD 步驟，前端合併進當前 testcase。

trace.zip 存於 PIC_FOLDER/recordings/{id}/trace.zip，並掛在 /pics 靜態路徑下。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.recording import RecordingSession
from app.schemas.recording import (
    ConvertResponse,
    GeneratedStep,
    RecorderCommandResponse,
    RecordingSessionCreate,
    RecordingSessionResponse,
)

log = logging.getLogger(__name__)

# Phase 1 docker 模式錄製的容器追蹤(in-memory,backend 重啟會孤兒)
# key=session_id;value=dict(container_id / host_port / vnc_password / started_at)
# 後續若要持久化,改加 RecordingSession 對應欄位
_recorder_containers: dict[str, dict] = {}

# Recorder image 自動 build 狀態(全域單例;同一時刻只會有一條 build 在跑)
# status 流向:
#   missing  → building → ready
#                    └→ error(可重試 → 回到 missing)
_recorder_image_state: dict = {
    "status": "unknown",     # unknown / missing / building / ready / error
    "log": [],               # 最近 200 行 build log 給前端 stream
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_recorder_image_lock = asyncio.Lock()  # 防止並發觸發 double-build

router = APIRouter()


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _session_dir(session_id: str) -> str:
    path = os.path.join(settings.PIC_FOLDER, "recordings", session_id)
    os.makedirs(path, exist_ok=True)
    return path


def _to_response(session: RecordingSession) -> RecordingSessionResponse:
    trace_url: Optional[str] = None
    if session.trace_path:
        # /pics 已掛在 main.py：StaticFiles(directory=PIC_FOLDER)
        trace_url = f"/pics/{session.trace_path}"
    return RecordingSessionResponse(
        id=session.id,
        project_id=session.project_id,
        target_url=session.target_url,
        status=session.status,
        script_text=session.script_text,
        trace_path=session.trace_path,
        trace_url=trace_url,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _build_commands(session_id: str, target_url: str) -> RecorderCommandResponse:
    upload_url = f"{settings.BASE_URL}/api/recordings/{session_id}/upload"
    # 注意：使用者在本機執行；輸出檔固定名稱便於上傳
    py = f"recorded_{session_id[:8]}.py"
    tz = f"trace_{session_id[:8]}.zip"
    short = session_id[:8]

    # Playwright ≥ 1.35 才支援 codegen 的 --save-trace 旗標；較舊版本會回報
    # "unknown option '--save-trace=...'" 而無法啟動 codegen。因此 one-liner
    # 會採「先試 --save-trace，失敗時退回無 trace 版本」的雙階段策略，並在
    # 上傳時動態偵測檔案是否存在，避免 curl 因找不到 trace.zip 而失敗。
    npx_cmd = (
        f'npx -y playwright codegen --target python '
        f'--save-trace="{tz}" -o "{py}" "{target_url}"'
    )
    pip_cmd = (
        f'python -m playwright codegen --target python '
        f'--save-trace="{tz}" -o "{py}" "{target_url}"'
    )
    pip_cmd_no_trace = (
        f'python -m playwright codegen --target python -o "{py}" "{target_url}"'
    )
    rf_cmd = f'rfbrowser codegen "{target_url}" -o {py}'

    host_root = (settings.RECORDER_HOST_ROOT or ".").strip()
    # 預設 "." 表示「不要切目錄，沿用使用者終端機目前所在位置」
    host_root_is_cwd = host_root in ("", ".", "./")

    # ── Windows 一鍵（PowerShell）────────────────────────────
    # 1) 切到 <project_root>\record\<sid>\ 工作目錄（避免在系統目錄執行）
    # 2) 先試 codegen --save-trace；若失敗退回不含 --save-trace 的版本
    # 3) curl 上傳；trace.zip 僅在確實存在時加入 -F 參數
    ps_cd = (
        ""
        if host_root_is_cwd
        else f'if (Test-Path "{host_root}") {{ Set-Location "{host_root}" }}; '
    )
    ps_script = (
        f'{ps_cd}'
        f'$wd = "record\\{short}"; '
        f'New-Item -ItemType Directory -Force -Path $wd | Out-Null; '
        f'Set-Location $wd; '
        # 強制 UTF-8 工作環境，避免 PS 5.1 預設 ANSI(cp950) 把中文/全形符號變成「?」
        f'$OutputEncoding = [System.Text.Encoding]::UTF8; '
        f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
        f'$env:PYTHONIOENCODING = "utf-8"; '
        f'$env:PYTHONUTF8 = "1"; '
        # ① 先試 --save-trace（Playwright ≥ 1.35 才支援）
        f'{pip_cmd}; '
        # 若 codegen 未成功產出 .py（例如舊版不認得 --save-trace），退回無 trace 版本
        f'if (-not (Test-Path "{py}")) {{ '
        f'  Write-Host "[info] --save-trace 不可用，改以無 trace 模式重試" -ForegroundColor Yellow; '
        f'  {pip_cmd_no_trace}; '
        f'}} '
        # ② 上傳前再次檢查；若連基本 codegen 都失敗則直接中止
        f'if (-not (Test-Path "{py}")) {{ '
        f'  Write-Host "[error] codegen 未產出 {py}，請確認 Playwright 已安裝" -ForegroundColor Red; '
        f'  exit 1; '
        f'}} '
        f'$curlArgs = @("-F", "script=@{py}"); '
        f'if (Test-Path "{tz}") {{ $curlArgs += @("-F", "trace=@{tz}") }} '
        f'else {{ Write-Host "[info] 未產生 trace.zip，僅上傳 script" -ForegroundColor Yellow }}; '
        f'$curlArgs += "{upload_url}"; '
        f'& curl.exe @curlArgs'
    )
    # 包成 -EncodedCommand：CMD / PowerShell / Windows Terminal 都能直接貼上執行
    import base64 as _b64
    _b = _b64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    ps_one_liner = f'powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {_b}'

    # ── macOS / Linux 一鍵（bash / zsh）──────────────────────
    # 用 POSIX sh 語法可同時在 bash/zsh 通吃。-e 讓 codegen 失敗時自動中止流程。
    bash_cd = "" if host_root_is_cwd else f'[ -d "{host_root}" ] && cd "{host_root}"; '
    bash_script = (
        f'export PYTHONIOENCODING=utf-8; export PYTHONUTF8=1; '
        f'{bash_cd}'
        f'wd="record/{short}"; mkdir -p "$wd"; cd "$wd"; '
        # ① 先試 --save-trace
        f'{pip_cmd} || true; '
        # ② 若沒產出 .py，退回無 trace 模式
        f'if [ ! -f "{py}" ]; then '
        f'  echo "[info] --save-trace 不可用，改以無 trace 模式重試"; '
        f'  {pip_cmd_no_trace} || true; '
        f'fi; '
        f'if [ ! -f "{py}" ]; then '
        f'  echo "[error] codegen 未產出 {py}，請確認 Playwright 已安裝" >&2; '
        f'  exit 1; '
        f'fi; '
        # ③ curl 上傳；trace 存在才帶 -F trace=@...
        f'args="-F script=@{py}"; '
        f'if [ -f "{tz}" ]; then args="$args -F trace=@{tz}"; '
        f'else echo "[info] 未產生 trace.zip，僅上傳 script"; fi; '
        f'curl $args "{upload_url}"'
    )
    # 直接輸出 shell 指令序列；使用者貼進 bash / zsh 終端機即可執行
    # 不用 bash -c '...' 包裝，避免 target_url 內含單引號時破壞外層引號
    bash_one_liner = bash_script

    # ── APP:啟 Appium server 一鍵指令(本機模式) ───────────────────
    # 使用者:
    #   1. 跑下面 npm/pip 指令(只一次性)
    #   2. 用 Appium Inspector 連 http://127.0.0.1:4723 開始錄製
    #   3. 錄完 Export → Python 腳本貼回 textarea 解析
    appium_install_npm = "npm install -g appium && appium driver install uiautomator2"
    appium_server_cmd = (
        f'{appium_install_npm}; appium --address 127.0.0.1 --port 4723 --base-path /'
    )
    appium_inspector_url = "https://github.com/appium/appium-inspector/releases"

    return RecorderCommandResponse(
        session_id=session_id,
        upload_url=upload_url,
        npx_command=npx_cmd,
        pip_command=pip_cmd,
        rfbrowser_command=rf_cmd,
        powershell_oneliner=ps_one_liner,
        bash_oneliner=bash_one_liner,
        appium_server_command=appium_server_cmd,
        appium_inspector_url=appium_inspector_url,
    )


# ─────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────
@router.post(
    "/recordings",
    response_model=dict,
    status_code=201,
    tags=["E · 錄製"],
)
async def create_recording(
    payload: RecordingSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    sid = str(uuid.uuid4())
    session = RecordingSession(
        id=sid,
        project_id=payload.project_id,
        target_url=payload.target_url,
        status="PENDING",
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return {
        "session": _to_response(session).model_dump(),
        "commands": _build_commands(sid, payload.target_url).model_dump(),
    }


@router.get(
    "/recordings",
    response_model=list[RecordingSessionResponse],
    tags=["E · 錄製"],
)
async def list_recordings(
    project_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """列出所有錄製 session（最新在前）。可選 project_id 過濾。"""
    stmt = select(RecordingSession).order_by(RecordingSession.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(RecordingSession.project_id == project_id)
    result = await db.execute(stmt)
    return [_to_response(s) for s in result.scalars().all()]


@router.get(
    "/recordings/{session_id}",
    response_model=RecordingSessionResponse,
    tags=["E · 錄製"],
)
async def get_recording(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    return _to_response(session)


@router.get(
    "/recordings/{session_id}/commands",
    response_model=RecorderCommandResponse,
    tags=["E · 錄製"],
)
async def get_recording_commands(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    return _build_commands(session.id, session.target_url)


@router.delete(
    "/recordings/{session_id}",
    status_code=204,
    tags=["E · 錄製"],
)
async def delete_recording(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    # 刪除目錄
    folder = _session_dir(session_id)
    for fn in os.listdir(folder):
        try:
            os.remove(os.path.join(folder, fn))
        except OSError:
            pass
    try:
        os.rmdir(folder)
    except OSError:
        pass
    await db.delete(session)
    await db.flush()


# ─────────────────────────────────────────────────────────
# Upload (multipart) — 由本機 codegen 完成後的 script + trace
# ─────────────────────────────────────────────────────────
@router.post(
    "/recordings/{session_id}/upload",
    response_model=RecordingSessionResponse,
    tags=["E · 錄製"],
)
async def upload_recording(
    session_id: str,
    script: Optional[UploadFile] = File(default=None),
    trace: Optional[UploadFile] = File(default=None),
    notes: Optional[str] = Form(default=None),  # noqa: ARG001 reserved
    db: AsyncSession = Depends(get_db),
):
    """
    接收 codegen 產生的檔案。任一檔案缺少均允許（部分上傳）。
    """
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")

    if script is not None:
        content = await script.read()
        # 安全:限制大小 1MB
        if len(content) > 1_000_000:
            raise HTTPException(413, "Script too large (>1MB)")
        try:
            session.script_text = content.decode("utf-8", errors="replace")
        except Exception:
            session.script_text = content.decode("latin-1", errors="replace")
        # script_text 已存 DB,不需另外寫到本地檔(會跟著 RecordingSession 一起)

    if trace is not None:
        content = await trace.read()
        if len(content) > 50_000_000:
            raise HTTPException(413, "Trace too large (>50MB)")
        # trace.zip 寫到 SeaweedFS,relative URL 寫到 session.trace_path
        from app.services.storage_service import save_bytes
        key = f"recordings/{session_id}/trace.zip"
        url = save_bytes(content, key, bucket="pic", content_type="application/zip")
        # trace_path 統一儲存 relative URL(/pics/recordings/<id>/trace.zip)
        # 而非 PIC_FOLDER 下的相對路徑 — 讓下載端直接 redirect 即可
        session.trace_path = url

    if session.script_text or session.trace_path:
        session.status = "UPLOADED"

    await db.flush()
    await db.refresh(session)
    return _to_response(session)


@router.get(
    "/recordings/{session_id}/trace",
    tags=["E · 錄製"],
)
async def download_trace(session_id: str, db: AsyncSession = Depends(get_db)):
    """重導向到 trace.zip 的物件儲存 URL。早期版本可能存舊式相對路徑(`recordings/<id>/trace.zip`),
    這裡為相容性處理:含 `/` 但不以 `/` 開頭代表是舊路徑,自動補上 `/pics/`。
    """
    from fastapi.responses import RedirectResponse
    session = await db.get(RecordingSession, session_id)
    if not session or not session.trace_path:
        raise HTTPException(404, "Trace not found")
    url = session.trace_path
    if not url.startswith(("/", "http://", "https://")):
        url = f"/pics/{url}"
    return RedirectResponse(url=url, status_code=302)


# ─────────────────────────────────────────────────────────
# Convert: Playwright codegen .py → BDD steps
# ─────────────────────────────────────────────────────────
# Playwright Python codegen 產生的常見呼叫範本：
#   page.goto("URL")
#   page.locator("SELECTOR").click()
#   page.get_by_role("button", name="送出").click()
#   page.get_by_label("帳號").fill("admin")
#   page.locator("#x").fill("v")
#   page.locator("#x").press("Enter")
#   expect(page.locator("h1")).to_contain_text("歡迎")
_PATTERNS = [
    # goto
    (
        re.compile(r'page\.goto\(\s*["\']([^"\']+)["\']\s*\)'),
        "goto",
    ),
    # locator(...).click()
    (
        re.compile(r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\.click\(\s*\)'),
        "click",
    ),
    # locator(...).fill("v")
    (
        re.compile(
            r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\.fill\(\s*["\']([^"\']*)["\']\s*\)'
        ),
        "fill",
    ),
    # locator(...).press("Enter")
    (
        re.compile(
            r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\.press\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "press",
    ),
    # get_by_role("button", name="送出").click()
    #   兼容 codegen 常見產物：
    #     - 多餘參數（exact=True / level=2 ...）
    #     - 鏈式呼叫（.first / .nth(0) / .last）
    (
        re.compile(
            r'page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'][^)]*\)'
            r'(?:\.(?:first|last|nth\(\s*\d+\s*\)))?\.click\(\s*\)'
        ),
        "role_click",
    ),
    # get_by_label("帳號").fill("admin")
    (
        re.compile(
            r'page\.get_by_label\(\s*["\']([^"\']+)["\']\s*\)\.fill\(\s*["\']([^"\']*)["\']\s*\)'
        ),
        "label_fill",
    ),
    # get_by_text("登入").click()
    (
        re.compile(r'page\.get_by_text\(\s*["\']([^"\']+)["\']\s*\)\.click\(\s*\)'),
        "text_click",
    ),
    # expect(...).to_contain_text("X")
    (
        re.compile(
            r'expect\(\s*page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)\.to_contain_text\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "assert_contain_text",
    ),
    # expect(...).to_have_text("X")
    (
        re.compile(
            r'expect\(\s*page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)\.to_have_text\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "assert_have_text",
    ),
    # expect(...).to_have_value("X")
    (
        re.compile(
            r'expect\(\s*page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)\.to_have_value\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "assert_have_value",
    ),
    # expect(...).to_be_visible()
    (
        re.compile(
            r'expect\(\s*page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)\.to_be_visible\(\s*\)'
        ),
        "assert_visible",
    ),
    # expect(...).to_be_checked()
    (
        re.compile(
            r'expect\(\s*page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)\.to_be_checked\(\s*\)'
        ),
        "assert_checked",
    ),
    # expect(page.get_by_role("...", name="...")).to_be_visible()  ─ codegen 在 Record assertion 後常見
    (
        re.compile(
            r'expect\(\s*page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'][^)]*\)\s*\)\.to_be_visible\(\s*\)'
        ),
        "assert_role_visible",
    ),
    # expect(page.get_by_role("...", name="...")).to_have_text("X")
    (
        re.compile(
            r'expect\(\s*page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'][^)]*\)\s*\)'
            r'\.to_have_text\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "assert_role_have_text",
    ),
    # expect(page.get_by_role("...", name="...")).to_contain_text("X")
    (
        re.compile(
            r'expect\(\s*page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'][^)]*\)\s*\)'
            r'\.to_contain_text\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "assert_role_contain_text",
    ),
    # page.locator("...").set_input_files("path")  ── 上傳檔案
    (
        re.compile(
            r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\.set_input_files\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "upload",
    ),
    # page.get_by_role("button", name="上傳").set_input_files("path")
    (
        re.compile(
            r'page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'][^)]*\)\s*'
            r'\.set_input_files\(\s*["\']([^"\']+)["\']\s*\)'
        ),
        "role_upload",
    ),
    # page.locator("...").drag_to(page.locator("..."))  ── 拖拉
    (
        re.compile(
            r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\.drag_to\(\s*'
            r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)\s*\)'
        ),
        "drag_to",
    ),
    # with page.expect_download() as download_info:
    #     page.get_by_role("link", name="xxx").click()
    #   → codegen 只會留下 click；我們把 expect_download 當成「提示標記」
    #     讓下一個 click 升格為 Download 動作
    (
        re.compile(r'page\.expect_download\(\s*\)'),
        "_download_hint",
    ),
    # with context.expect_page() as ... → 下一個 click 後會開新分頁，之後動作要 Switch Page
    (
        re.compile(r'context\.expect_page\(\s*\)'),
        "_new_tab_hint",
    ),
]


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_script(script: str) -> list[GeneratedStep]:
    steps: list[GeneratedStep] = []
    if not script:
        return steps

    # 「提示狀態」：用來偵測上一行看到的 expect_download / expect_page 會把「下一個 click」
    # 升格為 Download / SwitchTab。
    pending_hint: Optional[str] = None  # None / "download" / "new_tab"

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for pattern, kind in _PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            # 先處理「提示」類（不產 step，只設 flag 給下一行 click 用）
            if kind == "_download_hint":
                pending_hint = "download"
                break
            if kind == "_new_tab_hint":
                pending_hint = "new_tab"
                break
            if kind == "goto":
                url = m.group(1)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Given",
                        description=f"開啟頁面 {url}",
                        action="Goto",
                        locator="",
                        input=url,
                    )
                )
            elif kind == "click":
                sel = m.group(1)
                # 若前一行看到 expect_download()，此次 click 升格為 Download
                if pending_hint == "download":
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {sel} 下載檔案",
                            action="Download",
                            locator=sel,
                            input="/tmp/download",
                        )
                    )
                    pending_hint = None
                elif pending_hint == "new_tab":
                    # 點了之後會開新分頁：先 Click，再 SwitchTab 到 NEW
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {sel}（會開新分頁）",
                            action="Click",
                            locator=sel,
                        )
                    )
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description="切換到新分頁",
                            action="SwitchTab",
                            locator="",
                            input="NEW",
                        )
                    )
                    pending_hint = None
                else:
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {sel}",
                            action="Click",
                            locator=sel,
                        )
                    )
            elif kind == "fill":
                sel, val = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"於 {sel} 輸入「{val}」",
                        action="Fill",
                        locator=sel,
                        input=val,
                    )
                )
            elif kind == "press":
                sel, key = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"於 {sel} 按下 {key}",
                        action="Press",
                        locator=sel,
                        input=key,
                    )
                )
            elif kind == "role_click":
                role, name = m.group(1), m.group(2)
                loc = f'role={role}[name="{name}"]'
                if pending_hint == "download":
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {role}「{name}」下載檔案",
                            action="Download",
                            locator=loc,
                            input="/tmp/download",
                        )
                    )
                    pending_hint = None
                elif pending_hint == "new_tab":
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {role}「{name}」（會開新分頁）",
                            action="Click",
                            locator=loc,
                        )
                    )
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description="切換到新分頁",
                            action="SwitchTab",
                            locator="",
                            input="NEW",
                        )
                    )
                    pending_hint = None
                else:
                    steps.append(
                        GeneratedStep(
                            id=_new_id(),
                            keyword="When",
                            description=f"點擊 {role}「{name}」",
                            action="Click",
                            locator=loc,
                        )
                    )
            elif kind == "label_fill":
                label, val = m.group(1), m.group(2)
                loc = f'label={label}'
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"於「{label}」輸入「{val}」",
                        action="Fill",
                        locator=loc,
                        input=val,
                    )
                )
            elif kind == "text_click":
                txt = m.group(1)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"點擊文字「{txt}」",
                        action="Click",
                        locator=f'text={txt}',
                    )
                )
            elif kind == "assert_contain_text":
                sel, exp = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 應包含文字「{exp}」",
                        action="AssertText",
                        locator=sel,
                        condition="Contains",
                        expected=exp,
                    )
                )
            elif kind == "assert_have_text":
                sel, exp = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 文字應等於「{exp}」",
                        action="AssertText",
                        locator=sel,
                        condition="Equals",
                        expected=exp,
                    )
                )
            elif kind == "assert_have_value":
                sel, exp = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 值應等於「{exp}」",
                        action="AssertValue",
                        locator=sel,
                        condition="Equals",
                        expected=exp,
                    )
                )
            elif kind == "assert_visible":
                sel = m.group(1)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 應顯示於畫面",
                        action="AssertVisible",
                        locator=sel,
                        condition="IsVisible",
                        expected="true",
                    )
                )
            elif kind == "assert_checked":
                sel = m.group(1)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 應為勾選狀態",
                        action="AssertChecked",
                        locator=sel,
                        condition="IsChecked",
                        expected="true",
                    )
                )
            elif kind == "assert_role_visible":
                role, name = m.group(1), m.group(2)
                loc = f'role={role}[name="{name}"]'
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{role}「{name}」應顯示於畫面",
                        action="AssertVisible",
                        locator=loc,
                        condition="IsVisible",
                        expected="true",
                    )
                )
            elif kind == "assert_role_have_text":
                role, name, exp = m.group(1), m.group(2), m.group(3)
                loc = f'role={role}[name="{name}"]'
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{role}「{name}」文字應等於「{exp}」",
                        action="AssertText",
                        locator=loc,
                        condition="Equals",
                        expected=exp,
                    )
                )
            elif kind == "assert_role_contain_text":
                role, name, exp = m.group(1), m.group(2), m.group(3)
                loc = f'role={role}[name="{name}"]'
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{role}「{name}」應包含文字「{exp}」",
                        action="AssertText",
                        locator=loc,
                        condition="Contains",
                        expected=exp,
                    )
                )
            elif kind == "upload":
                sel, path = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"上傳檔案「{path}」至 {sel}",
                        action="Upload",
                        locator=sel,
                        input=path,
                    )
                )
            elif kind == "role_upload":
                role, name, path = m.group(1), m.group(2), m.group(3)
                loc = f'role={role}[name="{name}"]'
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"上傳檔案「{path}」至 {role}「{name}」",
                        action="Upload",
                        locator=loc,
                        input=path,
                    )
                )
            elif kind == "drag_to":
                src, dst = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="When",
                        description=f"把 {src} 拖到 {dst}",
                        action="DragAndDrop",
                        locator=src,
                        input=dst,
                    )
                )
            break  # 一行只匹配一個 pattern
    return steps


class AiEnhanceRequest(BaseModel):
    """Sprint 3.1 / 5.1 — AI 增強現有解析 step 陣列(可選 vision)。"""
    current_steps: list[dict] = []
    provider: Optional[str] = None
    # Sprint 5.1 — 是否從 trace.zip 抽 screenshot 餵 vision LLM
    # 限 GPT-4o / Claude 3.5 Sonnet / Gemini Pro 等支援 vision 的模型
    use_vision: bool = False


class AiEnhanceResponse(BaseModel):
    provider: str
    model: str
    original_count: int
    enhanced_count: int
    enhanced_steps: list[dict] = []
    vision_used: bool = False
    screenshot_count: int = 0
    raw: Optional[str] = None
    error: Optional[str] = None


@router.post(
    "/recordings/{session_id}/ai-enhance",
    response_model=AiEnhanceResponse,
    tags=["E · 錄製"],
)
async def ai_enhance_recording(
    session_id: str,
    payload: AiEnhanceRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sprint 3.1 — 把錄製腳本 + 已解析 step 餵 LLM,回增強版 step 陣列。
    一律走 preview,不直接改 session;前端呈現 diff 後使用者再決定接受/拒絕。
    """
    from app.services.ai_test_gen import enhance_steps_with_ai
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    if not (session.script_text or "").strip() and not payload.current_steps:
        raise HTTPException(409, "session 沒有 script_text 也沒給 current_steps,無法增強")
    try:
        result = await enhance_steps_with_ai(
            db,
            script_text=session.script_text or "",
            current_steps=payload.current_steps or [],
            provider=payload.provider,
            use_vision=payload.use_vision,
            trace_path=session.trace_path,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI 增強失敗:{e}")
    return result


@router.post(
    "/recordings/{session_id}/convert",
    response_model=ConvertResponse,
    tags=["E · 錄製"],
)
async def convert_recording(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    if not session.script_text:
        raise HTTPException(409, "尚未上傳 codegen 腳本或 HAR,無法轉換")
    text = session.script_text.lstrip()
    # API 模式上傳的是 HAR JSON;WEB 模式是 Playwright Python 腳本
    if text.startswith("{") and '"log"' in text[:200] and '"entries"' in text[:500]:
        steps = _parse_har_to_steps(session.script_text)
    else:
        steps = _parse_script(session.script_text)
    return ConvertResponse(steps=steps)


# ─────────────────────────────────────────────────────────
# Docker 模式錄製(Phase 1)
# 啟一個 autotest-recorder 容器,內含 Xvfb + noVNC + Playwright codegen;
# 把 noVNC port 對外 publish,前端用 iframe 嵌入該 URL 操作。
# ─────────────────────────────────────────────────────────


class DockerRecorderResponse(BaseModel):
    """啟動 docker 錄製容器後回給前端的資訊。"""
    session_id: str
    container_id: str
    container_name: str
    host_port: int
    vnc_password: str
    # 前端 iframe 用;組好的 noVNC lite client URL,密碼已 url-encoded 帶入
    novnc_path: str
    started_at: datetime
    expires_at: datetime


def _get_docker_client():
    """延後 import docker 套件 — 沒裝 / 沒 socket 時給友善錯誤。"""
    try:
        import docker  # type: ignore
    except ImportError:
        raise HTTPException(
            500,
            "後端缺少 docker 套件,無法使用 docker 模式錄製;"
            "請安裝 `pip install docker`(已列在 backend/requirements.txt)",
        )
    try:
        return docker.from_env()
    except Exception as e:
        raise HTTPException(
            500,
            f"無法連到 docker daemon:{e};"
            "請確認 backend container 有 mount /var/run/docker.sock",
        )


# ─── Recorder image 自動 build ───────────────────────────────────────

def _recorder_image_exists() -> bool:
    """同步檢查 image 是否存在(快;不需 lock)。"""
    try:
        client = _get_docker_client()
    except HTTPException:
        return False
    try:
        client.images.get(settings.RECORDER_IMAGE)
        return True
    except Exception:
        return False


def _append_build_log(line: str, max_lines: int = 200) -> None:
    """把一行 log 推進 state(限制最大長度避免無限累積)。"""
    state = _recorder_image_state
    state["log"].append(line)
    if len(state["log"]) > max_lines:
        state["log"] = state["log"][-max_lines:]


def _build_recorder_image_sync() -> None:
    """**Blocking** 的 build 流程,給 asyncio.to_thread 用。

    Build context 取自 backend image 內的 /app(Dockerfile 已 `COPY . /app`,
    所以 Dockerfile.recorder + tasks/recorder_entrypoint.sh 都在裡面)。
    Docker SDK 會把 path 整個打包成 tar 上傳到 daemon — daemon 不需要看到
    backend 容器內的檔案系統,只需要拿到 tar context 就能 build。
    """
    import docker  # type: ignore
    state = _recorder_image_state
    try:
        api = docker.APIClient(base_url="unix:///var/run/docker.sock")
    except Exception as e:
        state["status"] = "error"
        state["error"] = f"連不上 docker daemon:{e}"
        state["finished_at"] = datetime.utcnow()
        return

    state["status"] = "building"
    state["log"] = []
    state["error"] = None
    state["started_at"] = datetime.utcnow()
    state["finished_at"] = None
    _append_build_log(f"[backend] 開始 build {settings.RECORDER_IMAGE}")
    _append_build_log("[backend] context = /app, dockerfile = Dockerfile.recorder")

    try:
        # decode=True 把每個 chunk 解析成 dict;low-level api 讓我們即時讀 stream
        for chunk in api.build(
            path="/app",
            dockerfile="Dockerfile.recorder",
            tag=settings.RECORDER_IMAGE,
            rm=True,
            forcerm=True,
            decode=True,
            pull=False,
        ):
            if "stream" in chunk:
                for line in chunk["stream"].splitlines():
                    line = line.rstrip()
                    if line:
                        _append_build_log(line)
            elif "status" in chunk:
                # 拉 layer 進度;只記關鍵 step,不每個 byte 都打
                msg = chunk["status"]
                if "id" in chunk:
                    msg = f"{chunk['id']}: {msg}"
                _append_build_log(msg)
            elif "errorDetail" in chunk or "error" in chunk:
                err = chunk.get("errorDetail", {}).get("message") or chunk.get("error")
                _append_build_log(f"[error] {err}")
                state["status"] = "error"
                state["error"] = err
                state["finished_at"] = datetime.utcnow()
                return

        # build 結束 → 確認 image 真的存在
        if _recorder_image_exists():
            state["status"] = "ready"
            _append_build_log(f"[backend] build 完成,image={settings.RECORDER_IMAGE}")
        else:
            state["status"] = "error"
            state["error"] = "build 結束但 image 不存在(未知原因)"
        state["finished_at"] = datetime.utcnow()
    except Exception as e:
        log.exception("recorder image build failed")
        _append_build_log(f"[exception] {type(e).__name__}: {e}")
        state["status"] = "error"
        state["error"] = f"{type(e).__name__}: {e}"
        state["finished_at"] = datetime.utcnow()


async def _trigger_build_if_needed() -> str:
    """確保 build 在跑(若還沒跑且 image missing)。回傳當下 status。

    用 asyncio.Lock 避免兩個 request 同時觸發 double build。
    若已經 ready,直接回 ready,不啟新 build。
    若已在 building,直接回 building,不重啟。
    """
    async with _recorder_image_lock:
        state = _recorder_image_state
        if state["status"] == "building":
            return "building"
        if _recorder_image_exists():
            state["status"] = "ready"
            return "ready"
        # missing:啟非同步 build
        state["status"] = "building"
        # 在執行緒中跑(blocking I/O),不擋住 event loop
        asyncio.create_task(asyncio.to_thread(_build_recorder_image_sync))
        return "building"


class RecorderImageStatus(BaseModel):
    """Recorder image build 狀態(給前端 polling)。"""
    status: str  # missing / building / ready / error
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log_tail: list[str] = []


@router.get(
    "/recordings/recorder-image/status",
    response_model=RecorderImageStatus,
    tags=["E · 錄製"],
)
async def recorder_image_status():
    """查 recorder image 當前狀態 + build log tail。前端 polling 用。"""
    state = _recorder_image_state
    # 若 status=unknown 或 error 但可能 user 手動 build 過,再驗一次
    if state["status"] in ("unknown", "missing") and _recorder_image_exists():
        state["status"] = "ready"
    return RecorderImageStatus(
        status=state["status"] if state["status"] != "unknown"
            else ("ready" if _recorder_image_exists() else "missing"),
        error=state["error"],
        started_at=state["started_at"],
        finished_at=state["finished_at"],
        log_tail=list(state["log"][-80:]),  # 最後 80 行
    )


@router.post(
    "/recordings/recorder-image/build",
    response_model=RecorderImageStatus,
    tags=["E · 錄製"],
)
async def trigger_recorder_image_build():
    """觸發 recorder image build(若 image 已存在直接回 ready;
    若已在 build 中也不重啟,直接回 building)。"""
    new_status = await _trigger_build_if_needed()
    state = _recorder_image_state
    return RecorderImageStatus(
        status=new_status,
        error=state["error"],
        started_at=state["started_at"],
        finished_at=state["finished_at"],
        log_tail=list(state["log"][-80:]),
    )


@router.post(
    "/recordings/{session_id}/docker-start",
    response_model=DockerRecorderResponse,
    tags=["E · 錄製"],
)
async def docker_start(session_id: str, db: AsyncSession = Depends(get_db)):
    """啟一個 recorder 容器,回 noVNC iframe 可用的資訊。

    流程:
      1. 找 session,確認狀態
      2. 若該 session 已有跑著的容器 → 先 stop(避免 double-spawn)
      3. 啟新容器,Docker auto-assign host port 給 6080
      4. 反查實際 host port,組 noVNC URL 回前端
    """
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    if not session.target_url:
        raise HTTPException(400, "session 沒有 target_url,無法啟動 codegen")

    docker_client = _get_docker_client()

    # 先清掉舊的(若有)
    old = _recorder_containers.pop(session_id, None)
    if old:
        try:
            c = docker_client.containers.get(old["container_id"])
            c.remove(force=True)
        except Exception:
            pass

    vnc_password = secrets.token_urlsafe(12)
    upload_url = f"{settings.RECORDER_INTERNAL_BASE_URL.rstrip('/')}/api/recordings/{session_id}/upload"
    container_name = f"autotest-recorder-{session_id[:8]}"

    # Image 預檢:若 missing 直接觸發背景 build + 回 425 (Too Early),前端會
    # polling status,等 ready 後再 retry docker-start。避免 user 等同步 5-10
    # 分鐘 build 把 HTTP request 卡住。
    if not _recorder_image_exists():
        new_status = await _trigger_build_if_needed()
        # 若剛剛 image_exists() 才 false 但鎖住期間 status 變 ready(罕見 race),繼續
        if new_status != "ready":
            raise HTTPException(
                status_code=425,  # Too Early
                detail={
                    "code": "recorder_image_building",
                    "message": (
                        f"Recorder image 還沒 build 完(image={settings.RECORDER_IMAGE});"
                        "後端已自動開始 build,請在前端等待 progress 完成後再試。"
                    ),
                    "status": new_status,
                },
            )

    try:
        container = docker_client.containers.run(
            image=settings.RECORDER_IMAGE,
            name=container_name,
            detach=True,
            # auto_remove=True:容器 process 結束(codegen 退出 + 自動 curl 上傳完)
            # 後 docker daemon 自動 rm 容器,避免遺留 Exited 容器堆積。
            # 缺點:exited 容器立即消失,無法 docker logs 除錯;若要 debug
            # 把這個改成 False 即可。
            auto_remove=True,
            network=settings.RECORDER_NETWORK,
            ports={"6080/tcp": None},  # auto-assign host port
            environment={
                "TARGET_URL": session.target_url,
                "SESSION_ID": session_id,
                "UPLOAD_URL": upload_url,
                "VNC_PASSWORD": vnc_password,
            },
            labels={
                "autotest.role": "recorder",
                "autotest.session_id": session_id,
            },
        )
    except Exception as e:
        msg = str(e)
        if "No such image" in msg or "not found" in msg.lower():
            # 罕見:images.get 過了但 run 又說 missing(image 中途被刪)→ 觸發 rebuild
            await _trigger_build_if_needed()
            raise HTTPException(
                status_code=425,
                detail={
                    "code": "recorder_image_building",
                    "message": "Recorder image 不見了,已重新觸發 build,請稍後再試。",
                    "status": "building",
                },
            )
        raise HTTPException(500, f"啟動 recorder 容器失敗:{msg}")

    # Docker 對 6080 自動分配的 host port
    container.reload()
    port_info = (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).get("6080/tcp")
    if not port_info:
        # 啟容器但 port 沒映射成功 → 回收
        try:
            container.remove(force=True)
        except Exception:
            pass
        raise HTTPException(500, "容器啟動但 6080 port 未對外映射,請檢查 docker daemon 設定")
    host_port = int(port_info[0]["HostPort"])

    started_at = datetime.utcnow()
    expires_at = started_at + timedelta(minutes=settings.RECORDER_IDLE_TIMEOUT_MIN)

    _recorder_containers[session_id] = {
        "container_id": container.id,
        "container_name": container_name,
        "host_port": host_port,
        "vnc_password": vnc_password,
        "started_at": started_at,
        "expires_at": expires_at,
    }
    session.status = "RECORDING"
    await db.flush()

    # noVNC lite 連線 URL(查詢字串只是路徑,host 由前端用 window.location 填)
    # autoconnect=1 + reconnect=1 + resize=remote 是體驗最好的組合
    from urllib.parse import quote
    novnc_path = (
        f"/vnc_lite.html?path=websockify"
        f"&autoconnect=1&reconnect=1&resize=remote"
        f"&password={quote(vnc_password)}"
    )

    return DockerRecorderResponse(
        session_id=session_id,
        container_id=container.id,
        container_name=container_name,
        host_port=host_port,
        vnc_password=vnc_password,
        novnc_path=novnc_path,
        started_at=started_at,
        expires_at=expires_at,
    )


@router.post(
    "/recordings/{session_id}/docker-stop",
    status_code=204,
    tags=["E · 錄製"],
)
async def docker_stop(session_id: str, db: AsyncSession = Depends(get_db)):
    """停掉 session 對應的 recorder 容器。

    使用者按「停止錄製」時呼叫;若容器內 codegen 已先退出(自動上傳完),
    這個端點只是把容器移除。
    """
    info = _recorder_containers.pop(session_id, None)
    if not info:
        # 已經沒在跑了,不算錯誤
        return
    docker_client = _get_docker_client()
    try:
        c = docker_client.containers.get(info["container_id"])
        # 先 stop 給 entrypoint 機會 trap → upload(若還沒上傳);
        # auto_remove=True 之下 stop 後容器會自動 remove,不需手動 remove。
        try:
            c.stop(timeout=15)
        except Exception:
            pass
    except Exception as e:
        # 容器可能已被 auto_remove 自然清掉(codegen 自然退出後),不算錯誤
        log.info("docker_stop:container %s already gone (auto_remove): %s",
                 info.get("container_name"), e)

    # 回寫 session.status:若使用者中途停止且沒上傳成功,session 還是 PENDING
    session = await db.get(RecordingSession, session_id)
    if session and session.status == "RECORDING":
        # 如果已經 upload 過了 status 會被 upload endpoint 改成 UPLOADED
        # 還在 RECORDING 代表沒成功上傳 → 退回 PENDING 讓使用者可重來
        session.status = "PENDING"
        await db.flush()


# ─────────────────────────────────────────────────────────
# API 模式 Docker 錄製(mitmproxy / mitmweb)
# 跟 WEB 模式共用 _recorder_containers dict,但 key 加前綴 "api:" 避免衝突。
# ─────────────────────────────────────────────────────────


class ApiRecorderResponse(BaseModel):
    """API 模式啟容器後回的資訊;沒有 vnc_password 因為 mitmweb 無密碼。"""
    session_id: str
    container_id: str
    container_name: str
    proxy_port: int     # 容器外 host port,使用者把 HTTP proxy 設這個
    web_port: int       # 容器外 host port,前端 iframe 嵌入
    web_path: str       # mitmweb 的 path(預設 "/")
    started_at: datetime
    expires_at: datetime


def _api_key(session_id: str) -> str:
    return f"api:{session_id}"


@router.post(
    "/recordings/{session_id}/api-docker-start",
    response_model=ApiRecorderResponse,
    tags=["E · 錄製"],
)
async def api_docker_start(session_id: str, db: AsyncSession = Depends(get_db)):
    """啟一個 mitmproxy 容器,回 mitmweb iframe 與 proxy port 的資訊。"""
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")

    docker_client = _get_docker_client()

    # Image 預檢:autotest-recorder-api 不存在 → 回 425(前端去 polling 直到 build 完)
    try:
        docker_client.images.get(settings.RECORDER_API_IMAGE)
    except Exception:
        # 自動觸發 build(共用 image build 機制改寫;這裡先簡化:直接拋 425
        # 提示 user 跑 ./deploy.sh 或手動 build。後續再做 auto-build for api image)
        raise HTTPException(
            status_code=425,
            detail={
                "code": "recorder_api_image_missing",
                "message": (
                    f"找不到 image `{settings.RECORDER_API_IMAGE}`;"
                    "請跑 `./deploy.sh` / `deploy.ps1` 重新部署(已自動 build 此 image),"
                    "或手動執行:`docker build -f backend/Dockerfile.recorder-api "
                    "-t autotest-recorder-api:latest backend/`"
                ),
            },
        )

    key = _api_key(session_id)
    # 清掉舊的(若有)
    old = _recorder_containers.pop(key, None)
    if old:
        try:
            c = docker_client.containers.get(old["container_id"])
            c.remove(force=True)
        except Exception:
            pass

    upload_url = (
        f"{settings.RECORDER_INTERNAL_BASE_URL.rstrip('/')}"
        f"/api/recordings/{session_id}/upload-har"
    )
    container_name = f"autotest-recorder-api-{session_id[:8]}"

    try:
        container = docker_client.containers.run(
            image=settings.RECORDER_API_IMAGE,
            name=container_name,
            detach=True,
            auto_remove=True,
            network=settings.RECORDER_NETWORK,
            ports={
                "8080/tcp": None,  # proxy
                "8081/tcp": None,  # web UI
            },
            environment={
                "SESSION_ID": session_id,
                "UPLOAD_URL": upload_url,
            },
            labels={
                "autotest.role": "recorder-api",
                "autotest.session_id": session_id,
            },
        )
    except Exception as e:
        raise HTTPException(500, f"啟動 mitmproxy 容器失敗:{e}")

    container.reload()
    ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
    proxy_info = ports.get("8080/tcp")
    web_info = ports.get("8081/tcp")
    if not proxy_info or not web_info:
        try:
            container.remove(force=True)
        except Exception:
            pass
        raise HTTPException(500, "容器啟動但 8080/8081 未對外映射")
    proxy_port = int(proxy_info[0]["HostPort"])
    web_port = int(web_info[0]["HostPort"])

    started_at = datetime.utcnow()
    expires_at = started_at + timedelta(minutes=settings.RECORDER_IDLE_TIMEOUT_MIN)
    _recorder_containers[key] = {
        "container_id": container.id,
        "container_name": container_name,
        "proxy_port": proxy_port,
        "web_port": web_port,
        "started_at": started_at,
        "expires_at": expires_at,
    }
    session.status = "RECORDING"
    await db.flush()

    return ApiRecorderResponse(
        session_id=session_id,
        container_id=container.id,
        container_name=container_name,
        proxy_port=proxy_port,
        web_port=web_port,
        web_path="/",
        started_at=started_at,
        expires_at=expires_at,
    )


@router.post(
    "/recordings/{session_id}/api-docker-stop",
    status_code=204,
    tags=["E · 錄製"],
)
async def api_docker_stop(session_id: str, db: AsyncSession = Depends(get_db)):
    """停止 mitmproxy 容器(SIGTERM 觸發 entrypoint 把 HAR 上傳)。"""
    info = _recorder_containers.pop(_api_key(session_id), None)
    if not info:
        return
    docker_client = _get_docker_client()
    try:
        c = docker_client.containers.get(info["container_id"])
        try:
            # timeout=20 給 entrypoint trap 跑 curl 上傳 HAR
            c.stop(timeout=20)
        except Exception:
            pass
    except Exception as e:
        log.info("api_docker_stop:container %s already gone: %s",
                 info.get("container_name"), e)


@router.get(
    "/recordings/{session_id}/api-docker-status",
    response_model=Optional[ApiRecorderResponse],
    tags=["E · 錄製"],
)
async def api_docker_status(session_id: str):
    info = _recorder_containers.get(_api_key(session_id))
    if not info:
        return None
    try:
        docker_client = _get_docker_client()
        c = docker_client.containers.get(info["container_id"])
        if c.status not in ("running", "created"):
            _recorder_containers.pop(_api_key(session_id), None)
            return None
    except Exception:
        _recorder_containers.pop(_api_key(session_id), None)
        return None
    return ApiRecorderResponse(
        session_id=session_id,
        container_id=info["container_id"],
        container_name=info["container_name"],
        proxy_port=info["proxy_port"],
        web_port=info["web_port"],
        web_path="/",
        started_at=info["started_at"],
        expires_at=info["expires_at"],
    )


@router.post(
    "/recordings/{session_id}/upload-har",
    tags=["E · 錄製"],
)
async def upload_har(
    session_id: str,
    har: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """容器內 SIGTERM 收尾時 curl 上傳的 HAR 檔。

    存到 SeaweedFS,session.script_text 同時填入解析後的 JSON 摘要供前端
    convert 端點解析。
    """
    import json as _json
    session = await db.get(RecordingSession, session_id)
    if not session:
        raise HTTPException(404, "Recording session not found")
    raw = await har.read()
    if len(raw) > 50_000_000:
        raise HTTPException(413, "HAR 過大(>50MB)")
    if not raw:
        raise HTTPException(400, "HAR 是空的(代表沒擷取到任何流量)")
    # 存到 SeaweedFS
    try:
        from app.services.storage_service import save_bytes
        key = f"recordings/{session_id}/flows.har"
        save_bytes(raw, key, bucket="pic", content_type="application/json")
    except Exception as e:
        log.warning("HAR storage failed: %s", e)

    # 把 HAR JSON 整段塞進 script_text 讓 /convert 端點能解析(API 模式判定:
    # script_text 開頭是 `{` 表 HAR JSON,否則是 Playwright Python 腳本)
    try:
        # 驗證確實是合法 HAR JSON
        _ = _json.loads(raw.decode("utf-8", errors="replace"))
        session.script_text = raw.decode("utf-8", errors="replace")
        session.status = "UPLOADED"
    except Exception:
        raise HTTPException(400, "HAR JSON 解析失敗,可能容器內 addon 出錯")
    await db.flush()
    return {"ok": True, "size": len(raw)}


def _parse_har_to_steps(har_json: str) -> list[GeneratedStep]:
    """把 HAR JSON 解析成 Http.* 步驟。"""
    import json as _json
    try:
        data = _json.loads(har_json)
    except Exception:
        return []
    entries = (data.get("log") or {}).get("entries") or []
    steps: list[GeneratedStep] = []
    for entry in entries:
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        method = (req.get("method") or "GET").upper()
        url = req.get("url") or ""
        if not url:
            continue
        # 跳過明顯的靜態資源 / 預檢 OPTIONS
        if method == "OPTIONS":
            continue
        if any(url.lower().endswith(ext) for ext in (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
            ".css", ".woff", ".woff2", ".ttf", ".map",
        )):
            continue
        # body
        body = ""
        post = req.get("postData") or {}
        if isinstance(post, dict):
            body = post.get("text") or ""

        action = f"Http.{method.title() if method != 'GET' else 'Get'}"
        steps.append(GeneratedStep(
            id=str(uuid.uuid4()),
            keyword="When",
            description=f"{method} {url}",
            action=action,
            locator=url,
            input=body[:2000],  # 截短避免步驟過長
            condition="Equals",
            expected=str(resp.get("status") or ""),
        ))
    return steps


@router.get(
    "/recordings/{session_id}/docker-status",
    response_model=Optional[DockerRecorderResponse],
    tags=["E · 錄製"],
)
async def docker_status(session_id: str):
    """查目前 session 的 recorder 容器是否還活著(給前端 polling 用)。

    回 200 + DockerRecorderResponse 表示還在跑;
    回 200 + null 表示沒跑(已結束或從沒啟動)。
    """
    info = _recorder_containers.get(session_id)
    if not info:
        return None
    # 順便驗一下 docker daemon 真的還有這個容器(避免 backend 重啟造成幽靈)
    try:
        docker_client = _get_docker_client()
        c = docker_client.containers.get(info["container_id"])
        if c.status not in ("running", "created"):
            _recorder_containers.pop(session_id, None)
            return None
    except Exception:
        _recorder_containers.pop(session_id, None)
        return None
    return DockerRecorderResponse(
        session_id=session_id,
        container_id=info["container_id"],
        container_name=info["container_name"],
        host_port=info["host_port"],
        vnc_password=info["vnc_password"],
        novnc_path=(
            f"/vnc_lite.html?path=websockify"
            f"&autoconnect=1&reconnect=1&resize=remote"
            f"&password={info['vnc_password']}"
        ),
        started_at=info["started_at"],
        expires_at=info["expires_at"],
    )
