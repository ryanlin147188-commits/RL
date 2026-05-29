"""預設 Platform Skill seed — 每個 org 自動具備 9 個常用 skill。

Skill 是 per-org 的「對話模式 / playbook」(append 一段 system prompt + 限縮 LLM
可用的 tool 集合)。Frontend 對話框透過 chip 切換,讓使用者一鍵進入 BDD 寫測試 /
報告分析 / 缺陷追蹤等工作脈絡。

設計:
* 9 個預設 skill 寫成 markdown 檔(YAML frontmatter + body)放在
  ``default_skills/`` 同目錄,對齊 ``parse_skill_markdown`` 的格式。
* 啟動時對每個 organization 逐一 upsert(by name) — 不存在才建立,已存在就跳過
  (使用者修改過的版本不會被 seed 覆蓋)。
* idempotent:每次啟動跑都安全。
* 失敗不擋啟動(skill 是 UX 加成,不是系統必要元件) — 由 main.py 的 try/except
  保護,失敗只 log。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.organization import Organization
from app.services.skill_service import (
    SkillMarkdownInvalid,
    SkillNameConflict,
    get_skill_by_name,
    import_from_markdown,
)

logger = logging.getLogger(__name__)

# 同目錄下的 default_skills/ 子資料夾;每個 .md 檔對應一個 skill
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent / "default_skills"


def _load_skill_markdown_files() -> list[tuple[str, str]]:
    """掃 default_skills/*.md,回 [(filename, content), ...]。"""
    if not DEFAULT_SKILLS_DIR.is_dir():
        logger.warning(
            "default_skills directory not found at %s — skipping skill seed",
            DEFAULT_SKILLS_DIR,
        )
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(DEFAULT_SKILLS_DIR.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("read default skill %s failed: %s", path.name, exc)
            continue
        out.append((path.name, content))
    return out


async def _seed_one_org(
    session,
    *,
    organization_id: str,
    files: list[tuple[str, str]],
) -> tuple[int, int, int]:
    """對單一 org 跑 seed。回傳 (created, skipped, failed) 三個 count。"""
    created = 0
    skipped = 0
    failed = 0
    for filename, content in files:
        try:
            # 先 parse 看 name,若已存在就跳過(避免覆寫使用者改過的版本)
            from app.services.skill_service import parse_skill_markdown

            payload = parse_skill_markdown(content)
            existing = await get_skill_by_name(
                session, name=payload["name"], organization_id=organization_id
            )
            if existing is not None:
                skipped += 1
                continue
            # created_by=None — 系統 seed,沒有具體 user
            await import_from_markdown(
                session,
                organization_id=organization_id,
                created_by=None,
                content=content,
                overwrite=False,
            )
            created += 1
        except SkillNameConflict:
            # race condition:剛剛 check 完,別人也在 seed → 跳過
            skipped += 1
        except SkillMarkdownInvalid as exc:
            logger.warning(
                "default skill %s invalid markdown: %s", filename, exc
            )
            failed += 1
        except Exception as exc:  # noqa: BLE001 — seed 不該擋啟動
            logger.exception(
                "seed default skill %s into org %s failed: %s",
                filename,
                organization_id,
                exc,
            )
            failed += 1
    return created, skipped, failed


async def seed_default_skills(
    *, organization_id: Optional[str] = None
) -> None:
    """對指定 org(或全部 org)seed 預設 skill。

    給 ``main.py`` lifespan 用;也可從 admin endpoint 手動觸發針對單一 org backfill。

    每個 org 完成後 commit 一次,降低交易範圍;某 org 失敗不影響其他 org。
    """
    files = _load_skill_markdown_files()
    if not files:
        return

    async with AsyncSessionLocal() as session:
        if organization_id:
            org_ids: list[str] = [organization_id]
        else:
            rows = (await session.execute(select(Organization.id))).scalars().all()
            org_ids = list(rows)

        if not org_ids:
            logger.info("no organizations found — default skill seed skipped")
            return

        total_created = 0
        total_skipped = 0
        total_failed = 0
        for org_id in org_ids:
            try:
                c, s, f = await _seed_one_org(
                    session, organization_id=org_id, files=files
                )
                await session.commit()
                total_created += c
                total_skipped += s
                total_failed += f
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.exception(
                    "seed_default_skills org %s aborted: %s", org_id, exc
                )

        if total_created or total_failed:
            logger.info(
                "default skill seed done: created=%d skipped=%d failed=%d "
                "(across %d orgs)",
                total_created,
                total_skipped,
                total_failed,
                len(org_ids),
            )
