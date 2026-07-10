from typing import (
    Any,
)

from sqlalchemy.exc import IntegrityError
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
    MessageType,
)
from app.models.session import ChatSession
from app.models.user import User


class CRUDMessage(CRUDBase[Message, MessageCreate, MessageCreate]):
    async def get_by_dedupe_key(self, db: AsyncSession, dedupe_key: str) -> Message | None:
        result = await db.execute(select(Message).where(Message.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def create_idempotent(
        self,
        db: AsyncSession,
        *,
        obj_in: MessageCreate | dict[str, Any],
        dedupe_key: str,
    ) -> Message:
        obj_in_data = obj_in if isinstance(obj_in, dict) else obj_in.model_dump()
        db_obj = Message.model_validate({**obj_in_data, "dedupe_key": dedupe_key})
        db.add(db_obj)
        try:
            await db.commit()
            await db.refresh(db_obj)
            return db_obj
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_dedupe_key(db, dedupe_key)
            if existing is None:
                raise
            return existing

    async def get_by_session(self, db: AsyncSession, session_id: str, limit: int = 100) -> list[Message]:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc()).limit(limit))
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
        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)

        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_unprocessed_messages(self, db: AsyncSession, *, session_id: str, uid: str) -> list[Message]:
        """
        获取未处理的新消息（通常用于动态追加用户新输入的指令）
        """
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .where(Message.is_processed == False)  # noqa: E712
            .where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER)
            .order_by(Message.created_at.asc())
        )
        return result.scalars().all()

    async def get_history_paged(self, db: AsyncSession, *, session_id: str, uid: str, limit: int = 20, offset: int = 0) -> list[Message]:
        """
        用于前端分页加载会话历史记录
        """
        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER).order_by(Message.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_user_sessions(self, db: AsyncSession, uid: str = None, is_admin: bool = False) -> list[Any]:
        stmt = (
            select(
                Message.session_id,
                func.max(Message.created_at).label("last_active"),
                Message.uid,
                User.username,
                ChatSession.title,
                ChatSession.enable_markdown,
                ChatSession.source,
                ChatSession.reply_target_source,
                ChatSession.created_at,
            )
            .join(User, Message.uid == User.uid)
            .join(ChatSession, Message.session_id == ChatSession.session_id, isouter=True)
        )

        if not is_admin:
            stmt = stmt.where(Message.uid == uid)

        stmt = stmt.group_by(Message.session_id, Message.uid, User.username, ChatSession.title, ChatSession.enable_markdown, ChatSession.source, ChatSession.reply_target_source, ChatSession.created_at).order_by(desc("last_active"))
        result = await db.execute(stmt)
        return result.all()

    async def get_latest_session_profile_id(self, db: AsyncSession, *, session_id: str, uid: str) -> int | None:
        stmt = select(Message.profile_id).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER).order_by(Message.created_at.desc()).limit(1)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def remove_session(self, db: AsyncSession, session_id: str, uid: str = None, is_admin: bool = False) -> int:
        stmt = delete(Message).where(Message.session_id == session_id)
        if not is_admin:
            stmt = stmt.where(Message.uid == uid)
        result = await db.execute(stmt)
        deleted_count = result.rowcount or 0

        if deleted_count > 0:
            session_stmt = delete(ChatSession).where(ChatSession.session_id == session_id)
            if not is_admin:
                session_stmt = session_stmt.where(ChatSession.uid == uid)
            await db.execute(session_stmt)

        await db.commit()
        return deleted_count


message_crud = CRUDMessage(Message)
