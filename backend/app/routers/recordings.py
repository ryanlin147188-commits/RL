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

    # ⚠ playwright codegen 不支援 --save-trace，僅產生 script。
    # 若需 trace，請使用下方 powershell_oneliner（codegen → 注入 tracing → 重跑 → 上傳）。
    npx_cmd = f'npx -y playwright codegen --target python -o {py} "{target_url}"'
    pip_cmd = f'python -m playwright codegen --target python -o {py} "{target_url}"'
    rf_cmd = f'rfbrowser codegen "{target_url}" -o {py}'

    # 完整流程 (PowerShell)：
    # 1) codegen 產生 recorded.py (錄製過程不存 trace)
    # 2) 用 regex 注入 tracing.start/stop (在 new_context 之後與 browser.close 之前)
    # 3) 用 python 重跑修改後的 script → 產生 trace.zip
    # 4) curl 同時上傳 script + trace
    # 註：第 2 步若 codegen 用 'context = browser.new_context()' 命名才會成功。
    ps_script = (
        # 若專案根目錄存在，先切過去；確保「在任何位置貼指令」最後都落在 <root>\record\<sid>\
        # （避免在 C:\Windows\System32 等系統目錄執行時權限不足）
        f'if (Test-Path "{settings.RECORDER_HOST_ROOT}") {{ Set-Location "{settings.RECORDER_HOST_ROOT}" }}; '
        f'$wd = "record\\{session_id[:8]}"; '
        f'New-Item -ItemType Directory -Force -Path $wd | Out-Null; '
        f'Set-Location $wd; '
        # 強制以 UTF-8 工作，避免 PS 5.1 預設 ANSI(cp950) 讀寫時把中文/全形符號變成「?」亂碼
        f'$OutputEncoding = [System.Text.Encoding]::UTF8; '
        f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
        f'$env:PYTHONIOENCODING = "utf-8"; '
        f'$env:PYTHONUTF8 = "1"; '
        f'{pip_cmd}; '
        # ★ 以 UTF-8 讀進 codegen 產出的 .py，避免被當成 ANSI 解碼
        f'$src = Get-Content {py} -Raw -Encoding UTF8; '
        f'$src = $src '
        f'-replace \'(context\\s*=\\s*browser\\.new_context\\([^)]*\\))\', '
        f'"$1`n    context.tracing.start(screenshots=$true, snapshots=$true, sources=$true)" '
        f'-replace \'(\\s*)(browser\\.close\\(\\))\', '
        f'"`$1context.tracing.stop(path=\'\'{tz}\'\')`n`$1`$2"; '
        # ★ 以 UTF-8 (無 BOM) 寫回，確保 python 執行時也能正確讀取中文字串
        f'[System.IO.File]::WriteAllText((Resolve-Path {py}), $src, '
        f'(New-Object System.Text.UTF8Encoding $false)); '
        f'python {py}; '
        f'curl.exe -F "script=@{py}" -F "trace=@{tz}" {upload_url}'
    )
    # 包成 -EncodedCommand：在 CMD / PowerShell / Windows Terminal 貼上都能執行，
    # 完全免處理引號跳脫，避免使用者誤把 PS 語法貼進 CMD。
    import base64 as _b64
    _b = _b64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    one_liner = f'powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {_b}'
    return RecorderCommandResponse(
        session_id=session_id,
        upload_url=upload_url,
        npx_command=npx_cmd,
        pip_command=pip_cmd,
        rfbrowser_command=rf_cmd,
        powershell_oneliner=one_liner,
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
    (
        re.compile(
            r'page\.get_by_role\(\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\']\s*\)\.click\(\s*\)'
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
        "assert_text",
    ),
]


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_script(script: str) -> list[GeneratedStep]:
    steps: list[GeneratedStep] = []
    if not script:
        return steps

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for pattern, kind in _PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
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
            elif kind == "assert_text":
                sel, exp = m.group(1), m.group(2)
                steps.append(
                    GeneratedStep(
                        id=_new_id(),
                        keyword="Then",
                        description=f"{sel} 應顯示「{exp}」",
                        action="AssertText",
                        locator=sel,
                        expected=exp,
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
