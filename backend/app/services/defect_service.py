"""defect_service — defect 寫入 / 刪除的共用業務邏輯。

抽出 hard_delete 給 router (``DELETE /api/defects/{id}``) 與 AI agent tool
(``delete_defect``) 共用,避免兩邊各自實作 (容易出現 router 清掉 review_record
而 tool 沒清掉導致 dangling 紀錄這類差異)。
"""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.defect import Defect
from app.models.review import ReviewableEntityType, ReviewRecord


async def hard_delete(db: AsyncSession, defect: Defect) -> None:
    """硬刪除一筆 defect + 連動清掉所有指向它的 review_records。

    呼叫者要負責 ``await db.commit()``;這邊只 ``execute`` 不 commit,讓上游
    決定 transaction 邊界。
    """
    defect_id = defect.id
    await db.execute(
        delete(ReviewRecord)
        .where(ReviewRecord.entity_type == ReviewableEntityType.DEFECT)
        .where(ReviewRecord.entity_id == defect_id)
    )
    await db.execute(delete(Defect).where(Defect.id == defect_id))
