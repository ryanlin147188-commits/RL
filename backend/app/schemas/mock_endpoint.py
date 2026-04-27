"""Mock Endpoint Pydantic schemas — 取代前端 localStorage 存的 Mock 設定。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MockEndpointBase(BaseModel):
    name: str
    method: str = "GET"
    path: str
    description: Optional[str] = None
    enabled: bool = True
    status_code: int = 200
    delay_ms: int = 0
    response_headers_json: Optional[dict[str, Any]] = None
    response_body_text: Optional[str] = None
    request_headers_json: Optional[dict[str, Any]] = None
    request_body_text: Optional[str] = None


class MockEndpointCreate(MockEndpointBase):
    project_id: Optional[str] = None


class MockEndpointUpdate(BaseModel):
    name: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    status_code: Optional[int] = None
    delay_ms: Optional[int] = None
    response_headers_json: Optional[dict[str, Any]] = None
    response_body_text: Optional[str] = None
    request_headers_json: Optional[dict[str, Any]] = None
    request_body_text: Optional[str] = None


class MockEndpointResponse(MockEndpointBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
