from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import (
    delete,
    desc,
    func,
    select,
)

from app.core.crud.base import CRUDBase
from app.models.message import (
    Message,
    MessageCreate,
)
from app.models.user import User


class CRUDMessage(CRUDBase[Message, MessageCreate, MessageCreate]):
    async def get_by_session(
        self, db: AsyncSession, session_id: str, limit: int = 100
    ) -> list[Message]:
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_history(
        self, db: AsyncSession, *, session_id: str, uid: str, limit: int = 100
    ) -> list[Message]:
        """
        用于内部上下文管理器获取对话上下文；按时间倒序排列以便 ContextManager 进行 Token 窗口截断
        """
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_history_paged(self, db: AsyncSession, *, session_id: str,
        uid: str,
        limit: int = 20,
        offset: int = 0
    ) -> list[Message]:
        """
        用于前端分页加载会话历史记录
        """
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .order_by(Message.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_user_sessions(
        self, db: AsyncSession, uid: str = None, is_admin: bool = False
    ) -> list[Any]:
        stmt = select(
            Message.session_id,
            func.max(Message.created_at).label("last_active"),
            Message.uid,
            User.username,
        ).join(User, Message.uid == User.uid)

        if not is_admin:
            stmt = stmt.where(Message.uid == uid)

        stmt = stmt.group_by(Message.session_id, Message.uid, User.username).order_by(
            desc("last_active")
        )
        result = await db.execute(stmt)
        return result.all()

    async def remove_session(
        self, db: AsyncSession, session_id: str, uid: str = None, is_admin: bool = False
    ) -> int:
        stmt = delete(Message).where(Message.session_id == session_id)
        if not is_admin:
            stmt = stmt.where(Message.uid == uid)
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount


message_crud = CRUDMessage(Message)
