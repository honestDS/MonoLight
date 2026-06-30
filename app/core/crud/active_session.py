import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import delete

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
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
        如果发生 CancelledError，依然保证尝试执行释放逻辑。
        """
        async def _do_release():
            try:
                stmt = delete(ActiveSession).where(ActiveSession.session_id == session_id)
                await db.execute(stmt)
                await db.commit()
            except Exception:
                pass

        try:
            # 使用 shield 防止在释放期间响应外部取消，
            # 但 shield 本身被 await 时若当前 task 已是 cancelled 状态，会抛出异常，所以套层 try/except
            await asyncio.shield(_do_release())
        except asyncio.CancelledError:
            pass

    async def cleanup_expired_locks(self, db: AsyncSession, timeout_seconds: int = 300):
        """
        清理过期的锁（防止死锁）
        """
        deadline = get_local_time() - timedelta(seconds=timeout_seconds)
        stmt = delete(ActiveSession).where(ActiveSession.created_at < deadline)
        await db.execute(stmt)
        await db.commit()


active_session_crud = CRUDActiveSession(ActiveSession)
