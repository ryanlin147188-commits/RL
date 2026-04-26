"""通用工具：分頁參數、回應 helper。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query


@dataclass
class Pagination:
    """List endpoint 的分頁參數。

    用法：
        from app.common import Pagination, get_pagination

        @router.get("/things")
        async def list_things(
            page: Pagination = Depends(get_pagination),
            db: AsyncSession = Depends(get_db),
        ):
            stmt = select(Thing).order_by(Thing.created_at.desc())
            stmt = page.apply(stmt)
            rows = (await db.execute(stmt)).scalars().all()
            return rows

    回應仍為 array（不破壞既有 client）；client 透過 ?limit/offset 翻頁。
    """
    limit: int = 200
    offset: int = 0

    def apply(self, stmt):
        return stmt.limit(self.limit).offset(self.offset)


def get_pagination(
    limit: int = Query(200, ge=1, le=1000, description="單次最多回傳幾筆（1-1000）"),
    offset: int = Query(0, ge=0, description="跳過幾筆（給簡易翻頁用）"),
) -> Pagination:
    """FastAPI dependency；用 module-level function 取代 classmethod 以避免
    `Depends(Cls.classmethod)` 在 FastAPI 內省 signature 時不繫結 `cls` 的問題。"""
    return Pagination(limit=limit, offset=offset)


# 向後相容：保留 Pagination.from_query 屬性（指向同個函式）
Pagination.from_query = staticmethod(get_pagination)
