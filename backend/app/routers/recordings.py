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

import os
import re
import uuid
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

    return RecorderCommandResponse(
        session_id=session_id,
        upload_url=upload_url,
        npx_command=npx_cmd,
        pip_command=pip_cmd,
        rfbrowser_command=rf_cmd,
        powershell_oneliner=ps_one_liner,
        bash_oneliner=bash_one_liner,
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

    folder = _session_dir(session_id)

    if script is not None:
        content = await script.read()
        # 安全：限制大小 1MB
        if len(content) > 1_000_000:
            raise HTTPException(413, "Script too large (>1MB)")
        try:
            session.script_text = content.decode("utf-8", errors="replace")
        except Exception:
            session.script_text = content.decode("latin-1", errors="replace")
        with open(os.path.join(folder, "recorded.py"), "wb") as f:
            f.write(content)

    if trace is not None:
        content = await trace.read()
        if len(content) > 50_000_000:
            raise HTTPException(413, "Trace too large (>50MB)")
        trace_file = os.path.join(folder, "trace.zip")
        with open(trace_file, "wb") as f:
            f.write(content)
        session.trace_path = f"recordings/{session_id}/trace.zip"

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
    session = await db.get(RecordingSession, session_id)
    if not session or not session.trace_path:
        raise HTTPException(404, "Trace not found")
    full = os.path.join(settings.PIC_FOLDER, session.trace_path)
    if not os.path.exists(full):
        raise HTTPException(404, "Trace file missing on disk")
    return FileResponse(
        full,
        media_type="application/zip",
        filename=f"trace_{session_id[:8]}.zip",
    )


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
        raise HTTPException(409, "尚未上傳 codegen 腳本，無法轉換")
    steps = _parse_script(session.script_text)
    return ConvertResponse(steps=steps)
