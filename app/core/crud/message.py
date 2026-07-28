from typing import (
    Any,
)

from sqlalchemy import and_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import (
    delete,
    desc,
    func,
    select,
)

from app.core.constants import ERR_VALUE_MUST_BE_BETWEEN
from app.core.crud.base import CRUDBase
from app.core.i18n import t
from app.models.message import (
    Message,
    MessageCreate,
    MessageRole,
    MessageType,
)
from app.models.session import ChatSession
from app.models.user import User


class CRUDMessage(CRUDBase[Message, MessageCreate, MessageCreate]):
    async def create_guidance(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        profile_id: int,
        content: str,
        commit: bool = True,
    ) -> Message:
        message = Message(
            session_id=session_id,
            uid=uid,
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content=content,
            profile_id=profile_id,
            is_processed=False,
        )
        db.add(message)
        if commit:
            await db.commit()
            await db.refresh(message)
        else:
            await db.flush()
        return message

    async def activate_and_get_guidance_prompt(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
    ) -> str | None:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.uid == uid,
                Message.type == MessageType.GUIDANCE,
            )
            .order_by(Message.id.asc())
            .with_for_update()
        )
        messages = list(result.scalars().all())
        if not messages:
            return None

        pending_message_ids = [message.id for message in messages if message.id is not None and not message.is_processed]
        if pending_message_ids:
            await db.execute(update(Message).where(Message.id.in_(pending_message_ids)).values(is_processed=True).execution_options(synchronize_session=False))
        await db.flush()
        return messages[-1].content or None

    async def set_environment_prompt(self, db: AsyncSession, message_id: int, environment_prompt: str | None) -> bool:
        result = await db.execute(update(Message).where(Message.id == message_id).values(environment_prompt=environment_prompt))
        await db.commit()
        return bool(result.rowcount)

    async def get_by_dedupe_key(self, db: AsyncSession, dedupe_key: str) -> Message | None:
        result = await db.execute(select(Message).where(Message.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def get_latest_by_type(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        message_type: MessageType,
    ) -> Message | None:
        result = await db.execute(
            select(Message)
            .where(
                Message.uid == uid,
                Message.session_id == session_id,
                Message.type == message_type,
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def list_by_type(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        message_type: MessageType,
    ) -> list[Message]:
        result = await db.execute(
            select(Message)
            .where(
                Message.uid == uid,
                Message.session_id == session_id,
                Message.type == message_type,
            )
            .order_by(Message.id.desc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def update_content(
        self,
        db: AsyncSession,
        *,
        message_id: int,
        content: str,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(update(Message).where(Message.id == message_id).values(content=content).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def update_content_if_matches(
        self,
        db: AsyncSession,
        *,
        message_id: int,
        expected_content: str | None,
        content: str,
        message_type: MessageType = MessageType.AUDIT_CONFIRMATION,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(Message)
            .where(
                Message.id == message_id,
                Message.type == message_type,
                Message.content == expected_content,
            )
            .values(content=content)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def create_idempotent(
        self,
        db: AsyncSession,
        *,
        obj_in: MessageCreate | dict[str, Any],
        dedupe_key: str,
        commit: bool = True,
    ) -> Message:
        obj_in_data = obj_in if isinstance(obj_in, dict) else obj_in.model_dump()
        db_obj = Message.model_validate({**obj_in_data, "dedupe_key": dedupe_key})
        if commit:
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

        try:
            async with db.begin_nested():
                db.add(db_obj)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_dedupe_key(db, dedupe_key)
            if existing is None:
                raise
            return existing
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

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
        after_id: int | None = None,
    ) -> list[Message]:
        """
        用于内部上下文管理器获取对话上下文；按时间倒序排列以便 ContextManager 进行 Token 窗口截断
        """
        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.GUIDANCE)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        if after_id is not None:
            stmt = stmt.where(Message.id > after_id)

        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_history_forward_by_id(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        after_id: int | None = None,
        before_id: int | None = None,
        page_after_id: int | None = None,
        limit: int = 200,
    ) -> list[Message]:
        """
        按消息编号从旧到新读取固定开区间内的一页历史。
        """
        if not 1 <= limit <= 500:
            raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="limit", minimum=1, maximum=500))

        lower_bound = after_id
        if page_after_id is not None:
            lower_bound = page_after_id if lower_bound is None else max(lower_bound, page_after_id)

        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.GUIDANCE)
        if lower_bound is not None:
            stmt = stmt.where(Message.id > lower_bound)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)

        result = await db.execute(stmt.order_by(Message.id.asc()).limit(limit).execution_options(populate_existing=True))
        return result.scalars().all()

    async def get_history_backward_by_id(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        after_id: int | None = None,
        before_id: int | None = None,
        page_before_id: int | None = None,
        limit: int = 200,
    ) -> list[Message]:
        """
        按消息编号从新到旧读取固定开区间内的一页历史。
        """
        if not 1 <= limit <= 500:
            raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="limit", minimum=1, maximum=500))

        upper_bound = before_id
        if page_before_id is not None:
            upper_bound = page_before_id if upper_bound is None else min(upper_bound, page_before_id)

        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.GUIDANCE)
        if after_id is not None:
            stmt = stmt.where(Message.id > after_id)
        if upper_bound is not None:
            stmt = stmt.where(Message.id < upper_bound)

        result = await db.execute(stmt.order_by(Message.id.desc()).limit(limit).execution_options(populate_existing=True))
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
            .where(Message.type != MessageType.GUIDANCE)
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
        session_activity_stmt = select(
            Message.session_id.label("session_id"),
            func.max(Message.created_at).label("last_active"),
            Message.uid.label("uid"),
        )
        if not is_admin:
            session_activity_stmt = session_activity_stmt.where(Message.uid == uid)
        session_activity = session_activity_stmt.group_by(Message.session_id, Message.uid).subquery()
        last_active = func.coalesce(session_activity.c.last_active, ChatSession.created_at).label("last_active")

        stmt = (
            select(
                ChatSession.session_id,
                last_active,
                ChatSession.uid,
                User.username,
                ChatSession.title,
                ChatSession.enable_markdown,
                ChatSession.profile_id,
                ChatSession.profile_override_id,
                ChatSession.source,
                ChatSession.created_at,
                ChatSession.llm_request_metadata,
            )
            .join(User, ChatSession.uid == User.uid)
            .join(
                session_activity,
                and_(
                    session_activity.c.session_id == ChatSession.session_id,
                    session_activity.c.uid == ChatSession.uid,
                ),
                isouter=True,
            )
            .order_by(desc(last_active))
        )
        if not is_admin:
            stmt = stmt.where(ChatSession.uid == uid)
        result = await db.execute(stmt)
        return result.all()

    async def get_latest_session_profile_id(self, db: AsyncSession, *, session_id: str, uid: str) -> int | None:
        stmt = select(Message.profile_id).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER).where(Message.type != MessageType.GUIDANCE).order_by(Message.created_at.desc()).limit(1)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def remove_session(
        self,
        db: AsyncSession,
        session_id: str,
        uid: str = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
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

        if commit:
            await db.commit()
        return deleted_count


message_crud = CRUDMessage(Message)
