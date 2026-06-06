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
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[Message]:
        """
        用于内部上下文管理器获取对话上下文；按时间倒序排列以便 ContextManager 进行 Token 窗口截断
        """
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
        )
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)

        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_unprocessed_messages(
        self, db: AsyncSession, *, session_id: str, uid: str
    ) -> list[Message]:
        """
        获取未处理的新消息（通常用于动态追加用户新输入的指令）
        """
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .where(Message.is_processed == False)
            .order_by(Message.created_at.asc())
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
        from app.models.session import ChatSession
        stmt = select(
            Message.session_id,
            func.max(Message.created_at).label("last_active"),
            Message.uid,
            User.username,
            ChatSession.title,
        ).join(User, Message.uid == User.uid).join(
            ChatSession, Message.session_id == ChatSession.session_id, isouter=True
        )

        if not is_admin:
            stmt = stmt.where(Message.uid == uid)

        stmt = stmt.group_by(Message.session_id, Message.uid, User.username, ChatSession.title).order_by(
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
