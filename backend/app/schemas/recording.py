"""錄製功能 Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class RecordingSessionCreate(BaseModel):
    project_id: Optional[str] = None
    target_url: str


class RecordingSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: Optional[str]
    target_url: str
    status: str
    script_text: Optional[str] = None
    trace_path: Optional[str] = None
    trace_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class GeneratedStep(BaseModel):
    """符合 testcase_contents.steps_json 結構，可直接合併。"""

    id: str
    keyword: str = "When"
    description: str
    action: str
    locator: Optional[str] = ""
    input: Optional[str] = ""
    condition: Optional[str] = "Equals"
    expected: Optional[str] = ""


class ConvertResponse(BaseModel):
    steps: list[GeneratedStep]


class RecorderCommandResponse(BaseModel):
    """前端取得「使用者本機需貼上的指令」。"""

    session_id: str
    upload_url: str
    npx_command: str
    pip_command: str
    rfbrowser_command: str
    # Windows 一鍵（PowerShell）
    powershell_oneliner: str
    # macOS / Linux 一鍵（bash / zsh）
    bash_oneliner: str = ""
    # APP 平台:啟 Appium server + Inspector 提示
    appium_server_command: str = ""
    appium_inspector_url: str = ""
