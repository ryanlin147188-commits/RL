from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ImportJsonRequest(BaseModel):
    """POST /testcases/{node_id}/import-json 的請求體。"""
    ddt_json: dict[str, Any]


class TestcaseContentUpdate(BaseModel):
    ac_text: Optional[str] = None
    setup_text: Optional[str] = None
    steps_json: Optional[list[dict[str, Any]]] = None
    ddt_json: Optional[dict[str, Any]] = None


class TestcaseContentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    ac_text: Optional[str]
    setup_text: Optional[str]
    steps_json: Any
    ddt_json: Any
