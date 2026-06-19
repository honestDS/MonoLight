"""渠道加权轮询游标 CRUD：在多 worker 间共享并原子推进轮询位置"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.log import get_logger
from app.models.channel_cursor import ChannelCursor
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


class CRUDChannelCursor:
    async def next_index(self, cursor_key: str, modulo: int) -> int:
        """原子地取出当前轮询下标并推进游标。

        使用独立数据库会话，避免在调用方事务上执行 commit/rollback 破坏其事务边界。
        通过 with_for_update 行锁（MySQL/PostgreSQL 生效，SQLite 写操作天然串行）
        使多 worker / 多协程并发下加权轮询全局有序推进；首次创建的并发冲突由
        IntegrityError 捕获后重读处理。

        Args:
            cursor_key: 游标键（形如 "{profile_id}:{usage}:{priority}"）
            modulo: 展开序列长度（取模基数），必须 > 0

        Returns:
            本次应使用的展开序列下标（0 ~ modulo-1）
        """
        if modulo <= 0:
            return 0

        try:
            async with AsyncSessionLocal() as db:
                row = await self._get_or_create_locked(db, cursor_key)
                if row is None:
                    return 0

                idx = row.position % modulo
                row.position = (idx + 1) % modulo
                db.add(row)
                await db.commit()
                return idx
        except Exception as e:
            logger.bind(cursor_key=cursor_key).warning(f"读取轮询游标失败，回退使用下标 0: {e}")
            return 0

    async def _get_or_create_locked(self, db, cursor_key: str) -> ChannelCursor | None:
        """加行锁读取游标行；不存在则创建并处理并发首次创建冲突。"""
        stmt = select(ChannelCursor).where(ChannelCursor.cursor_key == cursor_key).with_for_update()
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row

        # 行不存在：插入初始游标，捕获并发首次创建导致的唯一键冲突
        db.add(ChannelCursor(cursor_key=cursor_key, position=0))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()

        # 重新加锁读取（无论本协程创建成功还是被其他协程抢先创建）
        return (await db.execute(stmt)).scalar_one_or_none()


channel_cursor_crud = CRUDChannelCursor()

