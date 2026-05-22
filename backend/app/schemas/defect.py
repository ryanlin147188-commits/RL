"""Defect (缺陷管理) Pydantic schemas。

對應 ``app.models.defect.Defect`` ORM,提供:
* ``DefectCreate``        — 手動新建缺陷
* ``DefectUpdate``        — PATCH;任何欄位可選,status 轉移走 router 端規則
* ``DefectFromReportRequest`` — 從失敗 ExecutionReport / step 一鍵建立
* ``DefectResponse``       — 列表/明細回傳;含 resolved linked_testcase / linked_report
* ``DefectAttachment``     — attachments_json 內的單筆結構(name/url/size/type)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.defect import DefectPriority, DefectSeverity, DefectStatus


class DefectAttachment(BaseModel):
    name: str
    url: str
    size: Optional[int] = None
    type: Optional[str] = None


class DefectCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=36)
    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    severity: DefectSeverity = DefectSeverity.MINOR
    priority: DefectPriority = DefectPriority.P2
    assignee: Optional[str] = Field(default=None, max_length=100)
    linked_testcase_id: Optional[str] = Field(default=None, max_length=36)
    linked_report_id: Optional[str] = Field(default=None, max_length=36)
    test_version_id: Optional[str] = Field(default=None, max_length=36)


class DefectUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    severity: Optional[DefectSeverity] = None
    priority: Optional[DefectPriority] = None
    status: Optional[DefectStatus] = None
    assignee: Optional[str] = Field(default=None, max_length=100)
    linked_testcase_id: Optional[str] = Field(default=None, max_length=36)
    linked_report_id: Optional[str] = Field(default=None, max_length=36)


class DefectFromReportRequest(BaseModel):
    """從失敗 ExecutionReport 一鍵建缺陷。

    後端會:
    1. 讀 ExecutionReport 拿 project_id;若 step_id 有給就讀對應 step
       拿 error_message → actual_result、screenshot URL → attachments[0]、
       testcase_node_id → linked_testcase_id。
    2. title 若沒 override 就用 ``"[失敗] <testcase name> / <step error 前 60 字>"``。
    """

    report_id: str = Field(..., min_length=1, max_length=36)
    step_id: Optional[str] = Field(default=None, max_length=36)
    title_override: Optional[str] = Field(default=None, max_length=300)
    severity: DefectSeverity = DefectSeverity.MAJOR
    priority: DefectPriority = DefectPriority.P1
    assignee: Optional[str] = Field(default=None, max_length=100)


class DefectLinkedRef(BaseModel):
    """linked_testcase / linked_report 在 response 內的精簡 resolved 形式。"""
    id: str
    code: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None


class DefectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    project_id: str
    title: str
    description: Optional[str] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    severity: DefectSeverity
    priority: DefectPriority
    status: DefectStatus
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    linked_testcase_id: Optional[str] = None
    linked_report_id: Optional[str] = None
    test_version_id: Optional[str] = None
    attachments_json: Optional[list[dict[str, Any]]] = None
    organization_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    # Router 在 read 時 batch-resolve;create 時可能為 None
    linked_testcase: Optional[DefectLinkedRef] = None
    linked_report: Optional[DefectLinkedRef] = None
