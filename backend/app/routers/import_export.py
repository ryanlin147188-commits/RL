"""Import / Export Markdown for testcase contents.

Routes:
* ``POST /api/testcases/{node_id}/import-md`` — body: ``{"markdown": "..."}``
* ``GET  /api/testcases/{node_id}/export-md``  — returns ``text/markdown``
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.testcase_content import TestcaseContent
from app.models.tree_node import LevelType, TreeNode
from app.schemas.testcase_content import TestcaseContentResponse
from app.services.markdown_service import parse_markdown, render_markdown


router = APIRouter()


class ImportMarkdownRequest(BaseModel):
    markdown: str


async def _load_testcase(db: AsyncSession, node_id: str) -> tuple[TreeNode, TestcaseContent | None]:
    node = await db.get(TreeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.level_type != LevelType.TESTCASE:
        raise HTTPException(status_code=400, detail="Node is not a TESTCASE type")
    result = await db.execute(select(TestcaseContent).where(TestcaseContent.node_id == node_id))
    return node, result.scalar_one_or_none()


@router.post("/testcases/{node_id}/import-md", response_model=TestcaseContentResponse)
async def import_markdown(node_id: str, payload: ImportMarkdownRequest, db: AsyncSession = Depends(get_db)):
    if not payload.markdown.strip():
        raise HTTPException(status_code=400, detail="markdown 內容不可為空")

    try:
        parsed = parse_markdown(payload.markdown)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _node, content = await _load_testcase(db, node_id)
    if content is None:
        content = TestcaseContent(node_id=node_id)
        db.add(content)

    content.ac_text = parsed["ac_text"]
    content.steps_json = parsed["steps_json"]
    content.ddt_json = parsed["ddt_json"]

    await db.flush()
    await db.refresh(content)
    return content


@router.get("/testcases/{node_id}/export-md", response_class=PlainTextResponse)
async def export_markdown(node_id: str, db: AsyncSession = Depends(get_db)):
    node, content = await _load_testcase(db, node_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Testcase content 尚未建立")

    md_text = render_markdown(
        test_case_name=node.name,
        ac_text=content.ac_text,
        steps_json=content.steps_json or [],
        ddt_json=content.ddt_json,
    )
    return PlainTextResponse(
        md_text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{node.name}.md"'},
    )
