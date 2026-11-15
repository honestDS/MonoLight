from dataclasses import dataclass
from typing import (
    Any,
)

from sqlalchemy import JSON, String, case, cast, exists, literal, or_, type_coerce, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import (
    delete,
    desc,
    func,
    select,
)

from app.core.constants import END_SESSION_TOOL_NAME, ERR_VALUE_MUST_BE_BETWEEN, TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY
from app.core.crud.base import CRUDBase
from app.core.crud.session.reply_stream_event import context_summary_running_expression
from app.core.i18n import t
from app.models.message import (
    Message,
    MessageCreate,
    MessageRole,
    MessageType,
)
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentWork
from app.models.user import User
from app.providers.database import ensure_sqlite_outer_transaction


@dataclass(frozen=True)
class SubagentNotificationCleanupResult:
    deleted: int = 0
    scanned: int = 0
    after_id: int = 0


class CRUDMessage(CRUDBase[Message, MessageCreate, MessageCreate]):
    async def persist(
        self,
        db: AsyncSession,
        *,
        message: Message,
        commit: bool = True,
        update_session_activity: bool = True,
    ) -> Message:
        db.add(message)
        await db.flush()
        if update_session_activity and message.work_id is None:
            await self._update_session_activity(db, message)
        if commit:
            await db.commit()
            await db.refresh(message)
        return message

    async def _update_session_activity(self, db: AsyncSession, message: Message) -> None:
        await db.execute(
            update(ChatSession)
            .where(
                ChatSession.session_id == message.session_id,
                ChatSession.uid == message.uid,
            )
            .values(
                latest_message_id=case(
                    (
                        or_(
                            ChatSession.latest_message_id.is_(None),
                            ChatSession.latest_message_id < message.id,
                        ),
                        message.id,
                    ),
                    else_=ChatSession.latest_message_id,
                ),
                last_message_at=case(
                    (
                        or_(
                            ChatSession.last_message_at.is_(None),
                            ChatSession.last_message_at < message.created_at,
                        ),
                        message.created_at,
                    ),
                    else_=ChatSession.last_message_at,
                ),
            )
            .execution_options(synchronize_session=False)
        )

    async def promote_work_message_to_session(
        self,
        db: AsyncSession,
        *,
        message_id: int,
        work_id: int,
        commit: bool = True,
    ) -> bool:
        message = await db.get(Message, message_id)
        if message is None or message.work_id not in {work_id, None}:
            return False
        if message.work_id == work_id:
            message.work_id = None
            db.add(message)
            await db.flush()
        await self._update_session_activity(db, message)
        session = await db.get(ChatSession, message.session_id)
        if session is not None and session.uid == message.uid:
            await db.refresh(session)
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True

    async def create(
        self,
        db: AsyncSession,
        *,
        obj_in: MessageCreate | dict[str, Any],
        update_dict: dict[str, Any] = None,
        commit: bool = True,
    ) -> Message:
        if isinstance(obj_in, dict):
            obj_in_data = obj_in
        else:
            obj_in_data = obj_in.model_dump()

        if update_dict:
            obj_in_data.update(update_dict)
        db_obj = Message.model_validate(obj_in_data)
        await self.persist(db, message=db_obj, commit=commit)
        if not commit:
            await db.refresh(db_obj)
        return db_obj

    async def count_by_sessions(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_ids: list[str],
    ) -> int:
        if not session_ids:
            return 0
        result = await db.execute(select(func.count()).select_from(Message).where(Message.uid == uid, Message.session_id.in_(session_ids)))
        return int(result.scalar() or 0)

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
        return await self.persist(db, message=message, commit=commit)

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

    async def update_model_context_suffix(
        self,
        db: AsyncSession,
        *,
        message_id: int,
        uid: str,
        session_id: str,
        model_context_suffix: str | None,
        only_if_unset: bool = False,
        commit: bool = True,
    ) -> bool:
        suffix_changed = (
            Message.model_context_suffix.is_not(None)
            if model_context_suffix is None
            else or_(
                Message.model_context_suffix.is_(None),
                Message.model_context_suffix != model_context_suffix,
            )
        )
        stmt = update(Message).where(
            Message.id == message_id,
            Message.uid == uid,
            Message.session_id == session_id,
            suffix_changed,
        )
        if only_if_unset:
            stmt = stmt.where(Message.model_context_suffix.is_(None))
        result = await db.execute(stmt.values(model_context_suffix=model_context_suffix).execution_options(synchronize_session=False))
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
        values: dict[str, Any] = {"content": content}
        if message_type == MessageType.TOOL_RESULT:
            values["model_context_suffix"] = None
        result = await db.execute(
            update(Message)
            .where(
                Message.id == message_id,
                Message.type == message_type,
                Message.content == expected_content,
            )
            .values(**values)
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
            try:
                await self.persist(db, message=db_obj, commit=True)
                return db_obj
            except IntegrityError:
                await db.rollback()
                existing = await self.get_by_dedupe_key(db, dedupe_key)
                if existing is None:
                    raise
                return existing

        await ensure_sqlite_outer_transaction(db)
        try:
            async with db.begin_nested():
                await self.persist(db, message=db_obj, commit=False)
        except IntegrityError:
            existing = await self.get_by_dedupe_key(db, dedupe_key)
            if existing is None:
                raise
            return existing
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def get_by_session(self, db: AsyncSession, session_id: str, limit: int = 100) -> list[Message]:
        result = await db.execute(select(Message).where(Message.session_id == session_id).where(Message.work_id.is_(None)).order_by(Message.created_at.asc()).limit(limit))
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
        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.work_id.is_(None)).where(Message.type != MessageType.GUIDANCE).where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        if after_id is not None:
            stmt = stmt.where(Message.id > after_id)

        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def list_audit_tool_results(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        audit_record_id: int,
    ) -> list[Message]:
        result = await db.execute(
            select(Message)
            .where(
                Message.uid == uid,
                Message.session_id == session_id,
                Message.profile_id == profile_id,
                Message.audit_record_id == audit_record_id,
                Message.role == MessageRole.TOOL,
                Message.type == MessageType.TOOL_RESULT,
                Message.audit_tool_call_id.is_not(None),
            )
            .order_by(Message.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def get_history_forward_by_id(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        work_id: int | None = None,
        profile_id: int | None = None,
        message_types: tuple[MessageType, ...] | None = None,
        lock_for_update: bool = False,
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

        work_filter = Message.work_id.is_(None) if work_id is None else Message.work_id == work_id
        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(work_filter).where(Message.type != MessageType.GUIDANCE).where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
        if profile_id is not None:
            stmt = stmt.where(Message.profile_id == profile_id)
        if message_types is not None:
            stmt = stmt.where(Message.type.in_(message_types))
        if lower_bound is not None:
            stmt = stmt.where(Message.id > lower_bound)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)

        stmt = stmt.order_by(Message.id.asc()).limit(limit).execution_options(populate_existing=True)
        if lock_for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        return result.scalars().all()

    async def list_late_cancelled_tool_results(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        source_message_ids: list[int],
        after_id: int,
    ) -> list[Message]:
        if not source_message_ids:
            return []

        messages: list[Message] = []
        source_message_id = Message.provider_metadata[TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY].as_integer()
        for index in range(0, len(source_message_ids), 200):
            source_batch = source_message_ids[index : index + 200]
            result = await db.execute(
                select(Message)
                .where(
                    Message.uid == uid,
                    Message.session_id == session_id,
                    Message.work_id.is_(None),
                    Message.role == MessageRole.TOOL,
                    Message.type == MessageType.TOOL_RESULT,
                    Message.id > after_id,
                    source_message_id.in_(source_batch),
                )
                .order_by(Message.id.asc())
            )
            messages.extend(result.scalars().all())

        messages.sort(key=lambda message: message.id)
        return messages

    async def list_recallable_chat_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        before_message_id: int | None = None,
        limit: int = 500,
    ) -> list[Message]:
        if not 1 <= limit <= 500:
            raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="limit", minimum=1, maximum=500))

        stmt = select(Message).where(Message.uid == uid).where(Message.work_id.is_(None)).where(Message.type == MessageType.TEXT).where(Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT))).where(Message.content.is_not(None)).where(func.trim(Message.content) != "")
        if before_message_id is not None:
            stmt = stmt.where(Message.id < before_message_id)

        result = await db.execute(stmt.order_by(Message.id.desc()).limit(limit))
        return list(result.scalars().all())

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

        stmt = select(Message).where(Message.session_id == session_id).where(Message.uid == uid).where(Message.work_id.is_(None)).where(Message.type != MessageType.GUIDANCE).where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
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
            .where(Message.work_id.is_(None))
            .where(Message.is_processed == False)  # noqa: E712
            .where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER)
            .where(Message.type != MessageType.GUIDANCE)
            .where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
            .order_by(Message.created_at.asc())
        )
        return result.scalars().all()

    async def get_history_paged(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        limit: int = 20,
        offset: int = 0,
        after_id: int | None = None,
        include_tool_messages: bool = True,
    ) -> list[Message]:
        """
        用于前端加载会话历史记录。
        after_id 为空时按倒序偏移分页；提供 after_id 时按消息编号正序增量读取。
        """
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .where(Message.work_id.is_(None))
            .where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER)
            .where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
            .where(Message.type != MessageType.SUBAGENT_RESULT)
            .where(
                or_(
                    Message.provider_metadata["internal_control"].as_string().is_(None),
                    Message.provider_metadata["internal_control"].as_string() != END_SESSION_TOOL_NAME,
                )
            )
        )
        if not include_tool_messages:
            stmt = stmt.where(Message.type.notin_((MessageType.TOOL_CALL, MessageType.TOOL_RESULT)))
        if after_id is not None:
            stmt = stmt.where(Message.id > after_id).order_by(Message.id.asc()).limit(limit)
        else:
            stmt = stmt.order_by(Message.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_user_sessions(self, db: AsyncSession, uid: str = None, is_admin: bool = False) -> list[Any]:
        last_active = func.coalesce(ChatSession.last_message_at, ChatSession.created_at).label("last_active")
        active_reply_work = select(1).where(
            SessionReplyWorkItem.session_id == ChatSession.session_id,
            SessionReplyWorkItem.uid == ChatSession.uid,
            SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
        )
        is_loading = exists(active_reply_work).label("is_loading")
        is_reply_running = exists(active_reply_work.where(SessionReplyWorkItem.work_type.in_([SessionReplyWorkType.FOREGROUND_REPLY, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION]))).label("is_reply_running")

        stmt = (
            select(
                ChatSession.session_id,
                last_active,
                ChatSession.latest_message_id,
                is_loading,
                is_reply_running,
                context_summary_running_expression(),
                ChatSession.uid,
                User.username,
                ChatSession.title,
                ChatSession.enable_markdown,
                ChatSession.stream_llm_request,
                ChatSession.goal_mode,
                ChatSession.max_turns,
                ChatSession.show_tool_calls,
                ChatSession.show_reasoning,
                ChatSession.profile_id,
                ChatSession.profile_override_id,
                ChatSession.source,
                ChatSession.message_platform_id,
                ChatSession.message_platform_user_id,
                ChatSession.created_at,
                ChatSession.llm_request_metadata,
                ChatSession.context_summary_revision,
                ChatSession.context_content_revision,
            )
            .join(User, ChatSession.uid == User.uid)
            .order_by(desc(last_active))
        )
        if not is_admin:
            stmt = stmt.where(ChatSession.uid == uid)
        result = await db.execute(stmt)
        return result.all()

    async def get_latest_session_profile_id(self, db: AsyncSession, *, session_id: str, uid: str) -> int | None:
        stmt = (
            select(Message.profile_id)
            .where(Message.session_id == session_id)
            .where(Message.uid == uid)
            .where(Message.work_id.is_(None))
            .where(Message.type != MessageType.SCHEDULED_TASK_TRIGGER)
            .where(Message.type != MessageType.GUIDANCE)
            .where(Message.type != MessageType.OUTBOUND_TEXT_REFINEMENT)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def cleanup_orphan_subagent_notifications(
        self,
        db: AsyncSession,
        *,
        limit: int,
        after_id: int = 0,
    ) -> SubagentNotificationCleanupResult:
        if limit <= 0:
            return SubagentNotificationCleanupResult()

        max_id_result = await db.execute(select(func.max(Message.id)))
        max_message_id = max_id_result.scalar_one_or_none()
        if max_message_id is None:
            return SubagentNotificationCleanupResult()

        page_result = await db.execute(select(Message.id).where(Message.id > after_id, Message.id < max_message_id).order_by(Message.id.asc()).limit(limit))
        page_ids = list(page_result.scalars().all())
        scanned = len(page_ids)
        if not page_ids:
            return SubagentNotificationCleanupResult()

        next_after_id = page_ids[-1] if scanned == limit else 0

        payload = type_coerce(
            case(
                (func.json_valid(Message.content) == 1, Message.content),
                else_="{}",
            ),
            JSON,
        )
        notification_type = payload["type"].as_string()
        notification_subagent_id = case(
            (notification_type == "external_work_resume", payload["results"][0]["subagent_id"].as_string()),
            else_=payload["subagent_id"].as_string(),
        )
        notification_work_id = case(
            (notification_type == "external_work_resume", payload["results"][0]["work_id"].as_integer()),
            else_=payload["work_id"].as_integer(),
        )

        linked_subagent_work = (
            select(1)
            .select_from(SubagentWork)
            .join(Subagent, Subagent.id == SubagentWork.subagent_id)
            .where(
                SubagentWork.child_work_id == notification_work_id,
                SubagentWork.subagent_id == notification_subagent_id,
                Subagent.uid == Message.uid,
                Subagent.session_id == Message.session_id,
                Subagent.profile_id == Message.profile_id,
            )
            .correlate(Message)
        )

        dialect_name = db.get_bind().dialect.name
        if dialect_name == "sqlite":
            pending_resume_message_ids = (
                func.json_each(
                    SessionReplyWorkCheckpoint.checkpoint,
                    literal("$.pending_resume_message_ids"),
                )
                .table_valued("value")
                .alias("pending_resume_message_id")
            )
            pending_id_matches = exists(select(1).select_from(pending_resume_message_ids).where(pending_resume_message_ids.c.value == Message.id).correlate(Message, SessionReplyWorkCheckpoint))
        else:
            pending_id_matches = (
                func.json_contains(
                    SessionReplyWorkCheckpoint.checkpoint,
                    cast(Message.id, String),
                    literal("$.pending_resume_message_ids"),
                )
                == 1
            )
        pending_reference = (
            select(1)
            .select_from(SessionReplyWorkCheckpoint)
            .join(SessionReplyWorkItem, SessionReplyWorkItem.id == SessionReplyWorkCheckpoint.work_id)
            .where(
                SessionReplyWorkItem.uid == Message.uid,
                SessionReplyWorkItem.session_id == Message.session_id,
                SessionReplyWorkItem.profile_id == Message.profile_id,
                pending_id_matches,
            )
            .correlate(Message)
        )

        candidate_conditions = [
            Message.is_processed.is_(True),
            Message.role == MessageRole.USER,
            Message.type == MessageType.SUBAGENT_RESULT,
            payload["source"].as_string() == "subagent",
            notification_type.in_(
                (
                    "subagent_board_update",
                    "subagent_result_ready",
                    "external_work_resume",
                )
            ),
            notification_subagent_id.is_not(None),
            func.trim(notification_subagent_id) != "",
            notification_work_id.is_not(None),
            notification_work_id > 0,
            # 保留全库最大 ID，避免 SQLite 删除尾部行后复用行号。
            Message.id < max_message_id,
            ~exists(linked_subagent_work),
            ~exists(pending_reference),
        ]

        result = await db.execute(delete(Message).where(Message.id.in_(page_ids), *candidate_conditions).execution_options(synchronize_session=False))
        return SubagentNotificationCleanupResult(deleted=result.rowcount or 0, scanned=scanned, after_id=next_after_id)

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
