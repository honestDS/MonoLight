from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import delete

from app.core.crud.base import CRUDBase
from app.models.active_session import ActiveSession


class CRUDActiveSession(CRUDBase[ActiveSession, ActiveSession, ActiveSession]):
    async def acquire_lock(self, db: AsyncSession, session_id: str) -> bool:
        """
        尝试获取会话锁。
        通过数据库唯一约束实现分布式排他性。
        """
        try:
            lock = ActiveSession(session_id=session_id)
            db.add(lock)
            await db.commit()
            return True
        except Exception:
            # 插入失败说明锁已存在
            await db.rollback()
            return False

    async def release_lock(self, db: AsyncSession, session_id: str):
        """
        释放会话锁
        """
        stmt = delete(ActiveSession).where(ActiveSession.session_id == session_id)
        await db.execute(stmt)
        await db.commit()

    async def cleanup_expired_locks(self, db: AsyncSession, timeout_seconds: int = 300):
        """
        清理过期的锁（防止死锁）
        """
        from datetime import timedelta

        from app.core.utils.time import get_local_time

        deadline = get_local_time() - timedelta(seconds=timeout_seconds)
        stmt = delete(ActiveSession).where(ActiveSession.created_at < deadline)
        await db.execute(stmt)
        await db.commit()


active_session_crud = CRUDActiveSession(ActiveSession)
