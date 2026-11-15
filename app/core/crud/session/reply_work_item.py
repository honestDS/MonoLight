import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import JSON, String, and_, case, cast, delete, exists, func, literal, or_, type_coerce, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.core.constants import (
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_SESSION_REPLY_CHECKPOINT_INVALID,
    ERR_SESSION_REPLY_DEDUPLICATION_FAILED,
    ERR_SESSION_REPLY_LEASE_LOST,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
    SESSION_REPLY_CLEANUP_BATCH_SIZE,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SESSION_REPLY_WORK_RETENTION_LIMIT,
)
from app.core.crud.session.reply_checkpoint_message import session_reply_checkpoint_message_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import AUDIT_TERMINAL_STATUSES, AuditRecord
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SESSION_REPLY_TERMINAL_STATUSES,
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall, SubagentWork

_TERMINAL_STATUS_VALUES = [status.value for status in SESSION_REPLY_TERMINAL_STATUSES]


def reply_work_is_protected(work: Any, *, dialect_name: str) -> Any:
    audit_record = (
        select(1)
        .select_from(AuditRecord)
        .where(
            AuditRecord.uid == work.uid,
            AuditRecord.session_id == work.session_id,
            AuditRecord.source_work_id == work.id,
            AuditRecord.status.not_in(AUDIT_TERMINAL_STATUSES),
        )
        .correlate(work)
    )
    active_audit_execution_work = aliased(SessionReplyWorkItem, name="active_audit_execution_work")
    audit_execution_work = (
        select(1)
        .select_from(active_audit_execution_work)
        .join(
            AuditRecord,
            and_(
                AuditRecord.source_work_id == work.id,
                AuditRecord.uid == work.uid,
                AuditRecord.session_id == work.session_id,
                active_audit_execution_work.source_id == cast(AuditRecord.id, String),
            ),
        )
        .where(
            active_audit_execution_work.uid == work.uid,
            active_audit_execution_work.session_id == work.session_id,
            active_audit_execution_work.profile_id == work.profile_id,
            active_audit_execution_work.status.in_([status.value for status in SESSION_REPLY_ACTIVE_STATUSES]),
            active_audit_execution_work.source_type == SessionReplySourceType.AUDIT_RECORD,
            active_audit_execution_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        )
        .correlate(work)
    )
    subagent_work = (
        select(1)
        .select_from(SubagentToolCall)
        .join(
            SubagentWork,
            and_(
                SubagentWork.child_work_id == SubagentToolCall.child_work_id,
                SubagentWork.subagent_id == SubagentToolCall.subagent_id,
            ),
        )
        .where(
            SubagentToolCall.parent_work_id == work.id,
            or_(
                SubagentToolCall.delivered.is_(False),
                SubagentWork.waiting_confirmation.is_(True),
                SubagentWork.result.is_(None),
            ),
        )
        .correlate(work)
    )

    terminal_delivery_ledger = BackgroundTask.extra["terminal_watch_deliveries"]
    terminal_delivery_path = literal('$.terminal_watch_deliveries."') + cast(work.id, String) + literal('"')
    terminal_delivery = type_coerce(
        func.json_extract(
            BackgroundTask.extra,
            terminal_delivery_path,
        ),
        JSON,
    )
    terminal_delivery_identity = terminal_delivery["identity"].as_string()
    terminal_delivery_consumed = terminal_delivery["consumed"].as_boolean()
    origin_match = and_(
        BackgroundTask.extra["origin_work_id"].as_integer() == work.id,
        BackgroundTask.extra["origin_work_identity"].as_string() == work.dedupe_key,
    )
    pending_terminal_delivery = or_(
        and_(
            terminal_delivery_identity == work.dedupe_key,
            terminal_delivery_consumed.is_(False),
        ),
        and_(terminal_delivery_ledger.as_string().is_(None), origin_match),
    )
    task_scope = (
        BackgroundTask.uid == work.uid,
        BackgroundTask.session_id == work.session_id,
        BackgroundTask.profile_id == work.profile_id,
    )

    def _waiting_slot_contains_task_id(field: str) -> Any:
        path = f"$.{field}"
        if dialect_name == "sqlite":
            slot = func.json_each(work.execution_state, literal(path)).table_valued("value").alias(f"reply_work_{field}_slot")
            return exists(select(1).select_from(slot).where(func.json_extract(slot.c.value, literal("$.task_id")) == BackgroundTask.id).correlate(work, BackgroundTask))
        return func.json_contains(work.execution_state, func.json_object("task_id", BackgroundTask.id), literal(path)) == 1

    waiting_slot = or_(*(_waiting_slot_contains_task_id(field) for field in ("pending_tools", "terminal_watch_waits")))
    active_origin_task = and_(
        BackgroundTask.status.in_([BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING]),
        BackgroundTask.extra["auto_terminal_watch"].as_boolean().is_not(True),
        origin_match,
    )
    replyable_external_task = and_(
        BackgroundTask.status.in_(
            [
                BackgroundTaskStatus.PENDING,
                BackgroundTaskStatus.RUNNING,
                BackgroundTaskStatus.SUCCEEDED,
                BackgroundTaskStatus.FAILED,
            ]
        ),
        BackgroundTask.auto_reply.is_(True),
        BackgroundTask.reply_status.in_([BackgroundTaskReplyStatus.PENDING, BackgroundTaskReplyStatus.RUNNING]),
        pending_terminal_delivery,
    )
    resumable_origin_task = and_(
        BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED, BackgroundTaskStatus.CANCELLED]),
        BackgroundTask.extra["auto_terminal_watch"].as_boolean().is_not(True),
        origin_match,
        waiting_slot,
        or_(
            BackgroundTask.extra["reply_owner"]["status"].as_string().is_(None),
            BackgroundTask.extra["reply_owner"]["status"].as_string() != "handled",
        ),
        or_(
            BackgroundTask.extra["terminal_watch_subscription"].as_boolean().is_not(True),
            BackgroundTask.status != BackgroundTaskStatus.CANCELLED,
        ),
    )
    external_task = (
        select(1)
        .select_from(BackgroundTask)
        .where(
            *task_scope,
            work.status.in_([SessionReplyWorkStatus.SUCCEEDED, SessionReplyWorkStatus.MERGED]),
            or_(active_origin_task, replyable_external_task, resumable_origin_task),
        )
        .correlate(work)
    )

    return or_(
        work.status.not_in(_TERMINAL_STATUS_VALUES),
        work.locked_by.is_not(None),
        work.lock_until.is_not(None),
        exists(audit_record),
        exists(audit_execution_work),
        exists(subagent_work),
        exists(external_task),
    )


def _work_category_expression(table: Any) -> Any:
    return case(
        (
            table.c.work_type == SessionReplyWorkType.AGENT_WORK,
            case(
                (
                    table.c.source_type == SessionReplySourceType.PARENT_WORK,
                    SessionReplyWorkCategory.SUBAGENT.value,
                ),
                else_=SessionReplyWorkCategory.SCHEDULED_TASK.value,
            ),
        ),
        else_=func.coalesce(
            table.c.execution_state["work_category"].as_string(),
            SessionReplyWorkCategory.FOREGROUND.value,
        ),
    )


def _is_subagent_board_review() -> Any:
    return SessionReplyWorkItem.execution_state["subagent_board_review_pending"].as_boolean().is_(True)


@dataclass(frozen=True)
class SessionReplyCleanupResult:
    work_items: int = 0
    stream_events: int = 0
    private_messages: int = 0
    sequences: int = 0
    checkpoint_messages: int = 0

    @property
    def total(self) -> int:
        return self.work_items + self.stream_events + self.private_messages + self.sequences + self.checkpoint_messages


class CRUDSessionReplyWorkItem:
    async def get(self, db: AsyncSession, work_id: int) -> SessionReplyWorkItem | None:
        result = await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == work_id))
        return result.scalars().first()

    async def _get_checkpoint_payload(self, db: AsyncSession, work_id: int) -> dict[str, Any] | None:
        result = await db.execute(select(SessionReplyWorkCheckpoint.checkpoint).where(SessionReplyWorkCheckpoint.work_id == work_id))
        return result.scalar_one_or_none()

    async def _save_checkpoint_payload(self, db: AsyncSession, work_id: int, checkpoint: dict[str, Any]) -> None:
        existing_checkpoint = await self._get_checkpoint_payload(db, work_id)
        if existing_checkpoint is None:
            db.add(SessionReplyWorkCheckpoint(work_id=work_id, checkpoint=checkpoint))
            await db.flush()
            return
        if existing_checkpoint == checkpoint:
            return
        await db.execute(update(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == work_id).values(checkpoint=checkpoint))

    async def get_checkpoint(self, db: AsyncSession, work_id: int) -> dict[str, Any] | None:
        checkpoint = await self._get_checkpoint_payload(db, work_id)
        if checkpoint is None:
            return None
        restored_checkpoint = deepcopy(checkpoint)
        message_keys: list[str] = []
        message_group_lengths: list[int] = []
        message_ids: list[int] = []
        for key in ("messages", "turn_messages"):
            if key not in restored_checkpoint:
                continue
            group_ids = restored_checkpoint[key]
            message_keys.append(key)
            message_group_lengths.append(len(group_ids))
            for message_id in group_ids:
                message_ids.append(message_id)
        if message_keys:
            loaded_messages = await session_reply_checkpoint_message_crud.load_messages(db, work_id, message_ids)
            offset = 0
            for index, key in enumerate(message_keys):
                group_length = message_group_lengths[index]
                restored_checkpoint[key] = loaded_messages[offset : offset + group_length]
                offset += group_length
        return restored_checkpoint

    async def get_checkpoint_metadata(self, db: AsyncSession, work_id: int) -> dict[str, Any] | None:
        checkpoint = await self._get_checkpoint_payload(db, work_id)
        if checkpoint is None:
            return None
        metadata: dict[str, Any] = {}
        for key, value in deepcopy(checkpoint).items():
            if key not in ("messages", "turn_messages"):
                metadata[key] = value
        return metadata

    async def save_checkpoint(self, db: AsyncSession, work_id: int, checkpoint: dict[str, Any]) -> None:
        # 调用方须在同一事务内先完成工作行条件写入或持有行锁，以串行化多执行者更新。
        lightweight_checkpoint: dict[str, Any] = {}
        message_keys: list[str] = []
        message_groups: list[list[dict[str, Any]]] = []
        for key, value in checkpoint.items():
            if key == "messages" or key == "turn_messages":
                if not isinstance(value, list):
                    raise ValueError(t(ERR_SESSION_REPLY_CHECKPOINT_INVALID))
                message_keys.append(key)
                message_groups.append(value)
            else:
                lightweight_checkpoint[key] = deepcopy(value)

        message_id_groups = await session_reply_checkpoint_message_crud.save_groups(
            db,
            work_id,
            "checkpoint",
            message_groups,
        )
        for index, key in enumerate(message_keys):
            lightweight_checkpoint[key] = message_id_groups[index]
        await self._save_checkpoint_payload(db, work_id, lightweight_checkpoint)

    async def save_checkpoint_metadata(self, db: AsyncSession, work_id: int, metadata: dict[str, Any]) -> None:
        if "messages" in metadata or "turn_messages" in metadata:
            raise ValueError(t(ERR_SESSION_REPLY_CHECKPOINT_INVALID))

        existing_checkpoint = await self._get_checkpoint_payload(db, work_id)
        lightweight_checkpoint: dict[str, Any] = {}
        if existing_checkpoint is not None:
            for key in ("messages", "turn_messages"):
                if key in existing_checkpoint:
                    lightweight_checkpoint[key] = deepcopy(existing_checkpoint[key])
        for key, value in metadata.items():
            lightweight_checkpoint[key] = deepcopy(value)
        await self._save_checkpoint_payload(db, work_id, lightweight_checkpoint)

    async def save_frozen_context(self, db: AsyncSession, work_id: int, messages: list[dict[str, Any]]) -> list[int]:
        message_id_groups = await session_reply_checkpoint_message_crud.save_groups(db, work_id, "frozen", [messages])
        return message_id_groups[0]

    async def get_frozen_context(self, db: AsyncSession, work: SessionReplyWorkItem) -> list[dict[str, Any]] | None:
        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        if "frozen_context_message_ids" not in execution_state:
            return None
        return await session_reply_checkpoint_message_crud.load_messages(
            db,
            work.id,
            execution_state["frozen_context_message_ids"],
        )

    async def delete_checkpoint(self, db: AsyncSession, work_id: int) -> None:
        # 调用方须在同一事务内先完成工作行条件写入或持有行锁，以串行化多执行者更新。
        await db.execute(delete(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == work_id))
        await session_reply_checkpoint_message_crud.delete_references(db, work_id, "checkpoint")

    async def get_audit_source_work(
        self,
        db: AsyncSession,
        *,
        audit_record_id: int,
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> SessionReplyWorkItem | None:
        result = await db.execute(
            select(SessionReplyWorkItem)
            .join(AuditRecord, AuditRecord.source_work_id == SessionReplyWorkItem.id)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.uid == uid,
                AuditRecord.session_id == session_id,
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
            )
        )
        return result.scalars().first()

    async def get_message_work_id(self, db: AsyncSession, work: SessionReplyWorkItem) -> int | None:
        if work.work_type == SessionReplyWorkType.AGENT_WORK:
            return work.id
        if work.work_type != SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION or work.source_type != SessionReplySourceType.AUDIT_RECORD:
            return None
        try:
            audit_record_id = int(work.source_id)
        except (TypeError, ValueError):
            return None

        result = await db.execute(
            select(Message.work_id)
            .join(AuditRecord, AuditRecord.source_assistant_message_id == Message.id)
            .where(
                AuditRecord.id == audit_record_id,
                AuditRecord.uid == work.uid,
                AuditRecord.session_id == work.session_id,
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.ASSISTANT,
                Message.type == MessageType.TOOL_CALL,
                Message.profile_id == work.profile_id,
                Message.work_id.is_not(None),
            )
        )
        return work.id if result.scalar() is not None else None

    async def get_by_dedupe_key(self, db: AsyncSession, dedupe_key: str) -> SessionReplyWorkItem | None:
        result = await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def get_by_input_message(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        message_id: int,
    ) -> SessionReplyWorkItem | None:
        result = await db.execute(
            select(SessionReplyWorkItem).where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                or_(
                    and_(
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
                        SessionReplyWorkItem.source_type == SessionReplySourceType.USER_MESSAGE,
                        SessionReplyWorkItem.source_id == str(message_id),
                    ),
                    and_(
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                        SessionReplyWorkItem.execution_state["decision_message_id"].as_integer() == message_id,
                    ),
                ),
            )
        )
        return result.scalars().first()

    async def has_active_interactive_work(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
    ) -> bool:
        result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.work_type.in_(
                    [
                        SessionReplyWorkType.FOREGROUND_REPLY,
                        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                    ]
                ),
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def list_recent_http_for_sessions(
        self,
        db: AsyncSession,
        *,
        uid: str,
        is_admin: bool = False,
        session_ids: list[str],
        limit_per_session: int = 30,
    ) -> list[SessionReplyWorkItem]:
        if not session_ids:
            return []

        common_conditions = [
            SessionReplyWorkItem.session_id.in_(session_ids),
            SessionReplyWorkItem.work_type.in_(
                [
                    SessionReplyWorkType.FOREGROUND_REPLY,
                    SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                ]
            ),
            SessionReplyWorkItem.execution_state["message_source"].as_string() == "http",
        ]
        if not is_admin:
            common_conditions.append(SessionReplyWorkItem.uid == uid)
        terminal_ranked = (
            select(
                SessionReplyWorkItem.id.label("work_id"),
                func.row_number()
                .over(
                    partition_by=SessionReplyWorkItem.session_id,
                    order_by=SessionReplyWorkItem.sequence_no.desc(),
                )
                .label("terminal_row_number"),
            )
            .where(
                *common_conditions,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_TERMINAL_STATUSES),
                SessionReplyWorkItem.updated_at >= get_local_time() - timedelta(hours=24),
            )
            .subquery()
        )
        result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                *common_conditions,
                or_(
                    SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
                    SessionReplyWorkItem.id.in_(select(terminal_ranked.c.work_id).where(terminal_ranked.c.terminal_row_number <= limit_per_session)),
                ),
            )
            .order_by(SessionReplyWorkItem.session_id.desc(), SessionReplyWorkItem.sequence_no.desc())
        )
        return list(result.scalars().all())

    async def get_next_for_session_resume(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        history_message_id: int,
        after_sequence_no: int,
        interactive_only: bool = False,
    ) -> SessionReplyWorkItem | None:
        unseen_turn = exists(
            select(SessionReplyStreamEvent.id).where(
                SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id,
                SessionReplyStreamEvent.event["type"].as_string() == "turn_end",
                SessionReplyStreamEvent.event["message_id"].as_integer() > history_message_id,
            )
        )
        conditions = [
            SessionReplyWorkItem.uid == uid,
            SessionReplyWorkItem.session_id == session_id,
            SessionReplyWorkItem.sequence_no > after_sequence_no,
            SessionReplyWorkItem.status != SessionReplyWorkStatus.MERGED,
        ]
        visibility_conditions = [
            SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            SessionReplyWorkItem.result_message_id > history_message_id,
            unseen_turn,
        ]
        if interactive_only:
            conditions.append(
                SessionReplyWorkItem.work_type.in_(
                    [
                        SessionReplyWorkType.FOREGROUND_REPLY,
                        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                    ]
                )
            )
            visibility_conditions.append(
                and_(
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.CANCELLED,
                    SessionReplyWorkItem.result_message_id.is_(None),
                )
            )
        result = await db.execute(select(SessionReplyWorkItem).where(*conditions, or_(*visibility_conditions)).order_by(SessionReplyWorkItem.sequence_no, SessionReplyWorkItem.id).limit(1))
        return result.scalars().first()

    async def allocate_sequence_no(self, db: AsyncSession, session_id: str) -> int:
        now = get_local_time()
        statement = (
            sqlite_insert(SessionReplySequence)
            .values(session_id=session_id, next_sequence_no=2, updated_at=now)
            .on_conflict_do_update(
                index_elements=[SessionReplySequence.session_id],
                set_={
                    "next_sequence_no": SessionReplySequence.next_sequence_no + 1,
                    "updated_at": now,
                },
            )
            .returning(SessionReplySequence.next_sequence_no)
        )
        result = await db.execute(statement)
        return int(result.scalar_one()) - 1

    async def has_pending_user_input(self, db: AsyncSession, *, uid: str, session_id: str) -> bool:
        result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
                SessionReplyWorkItem.source_type == SessionReplySourceType.USER_MESSAGE,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                SessionReplyWorkItem.input_message_ids.is_(None),
                _work_category_expression(SessionReplyWorkItem.__table__) == SessionReplyWorkCategory.FOREGROUND.value,
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def defer_ready_board_reviews(self, db: AsyncSession, *, uid: str, session_id: str) -> None:
        pending_user_input = SessionReplyWorkItem.__table__.alias("pending_user_input")
        pending_user_input_category = _work_category_expression(pending_user_input)
        max_pending_user_input_sequence = (
            select(func.max(pending_user_input.c.sequence_no))
            .where(
                pending_user_input.c.uid == uid,
                pending_user_input.c.session_id == session_id,
                pending_user_input.c.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
                pending_user_input.c.source_type == SessionReplySourceType.USER_MESSAGE,
                pending_user_input.c.status == SessionReplyWorkStatus.READY_FOR_LLM,
                pending_user_input.c.input_message_ids.is_(None),
                pending_user_input_category == SessionReplyWorkCategory.FOREGROUND.value,
            )
            .scalar_subquery()
        )
        result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                _work_category_expression(SessionReplyWorkItem.__table__) == SessionReplyWorkCategory.FOREGROUND.value,
                _is_subagent_board_review(),
                SessionReplyWorkItem.sequence_no < max_pending_user_input_sequence,
            )
            .order_by(SessionReplyWorkItem.sequence_no)
        )
        for work_id in result.scalars().all():
            sequence_no = await self.allocate_sequence_no(db, session_id)
            await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id == work_id,
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                )
                .values(sequence_no=sequence_no, updated_at=get_local_time())
            )

    async def yield_board_review(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        expired_at: int | None = None,
        commit: bool = True,
    ) -> bool:
        conditions = [
            SessionReplyWorkItem.id == work_id,
            SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
            SessionReplyWorkItem.locked_by == worker_id,
            _is_subagent_board_review(),
            _work_category_expression(SessionReplyWorkItem.__table__) == SessionReplyWorkCategory.FOREGROUND.value,
        ]
        if expired_at is not None:
            conditions.append(or_(SessionReplyWorkItem.lock_until.is_(None), SessionReplyWorkItem.lock_until <= expired_at))
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(*conditions)
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=int(time.time()),
                attempt_count=SessionReplyWorkItem.attempt_count - 1,
                updated_at=get_local_time(),
            )
            .returning(SessionReplyWorkItem.uid, SessionReplyWorkItem.session_id)
        )
        yielded = result.first()
        if yielded is None:
            if commit:
                await db.rollback()
            return False

        _, session_id = yielded
        sequence_no = await self.allocate_sequence_no(db, session_id)
        await db.execute(update(SessionReplyWorkItem).where(SessionReplyWorkItem.id == work_id).values(sequence_no=sequence_no, updated_at=get_local_time()))
        if commit:
            await db.commit()
        return True

    async def enqueue(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        work_type: SessionReplyWorkType,
        source_type: SessionReplySourceType,
        source_id: str | int,
        dedupe_key: str,
        work_category: SessionReplyWorkCategory | None = None,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        resolved_work_category = work_category
        if resolved_work_category is None:
            if work_type == SessionReplyWorkType.AGENT_WORK:
                if source_type == SessionReplySourceType.PARENT_WORK:
                    resolved_work_category = SessionReplyWorkCategory.SUBAGENT
                else:
                    resolved_work_category = SessionReplyWorkCategory.SCHEDULED_TASK
            else:
                resolved_work_category = SessionReplyWorkCategory.FOREGROUND

        async with db.begin_nested() as savepoint:
            sequence_no = await self.allocate_sequence_no(db, session_id)
            now = get_local_time()
            statement = (
                sqlite_insert(SessionReplyWorkItem)
                .values(
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    sequence_no=sequence_no,
                    work_type=work_type,
                    source_type=source_type,
                    source_id=str(source_id),
                    dedupe_key=dedupe_key,
                    status=SessionReplyWorkStatus.READY_FOR_LLM,
                    execution_state={"work_category": resolved_work_category.value},
                    event_sent=False,
                    attempt_count=0,
                    max_attempts=2,
                    available_at=int(time.time()),
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[SessionReplyWorkItem.dedupe_key])
                .returning(SessionReplyWorkItem)
            )
            result = await db.execute(statement)
            work = result.scalars().first()
            if work is not None and work_type == SessionReplyWorkType.FOREGROUND_REPLY and source_type == SessionReplySourceType.USER_MESSAGE:
                await self.defer_ready_board_reviews(db, uid=uid, session_id=session_id)
            if work is None:
                await savepoint.rollback()
        if work is None:
            work = await self.get_by_dedupe_key(db, dedupe_key)
            if work is None:
                raise RuntimeError(t(ERR_SESSION_REPLY_DEDUPLICATION_FAILED))
            if commit:
                await db.commit()
            return work, False
        if commit:
            await db.commit()
        return work, True

    async def has_nonterminal_predecessor(
        self,
        db: AsyncSession,
        work: SessionReplyWorkItem,
    ) -> bool:
        if work.id is None or work.sequence_no is None or work.work_category != SessionReplyWorkCategory.FOREGROUND:
            return False
        work_category = _work_category_expression(SessionReplyWorkItem.__table__)
        result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.sequence_no < work.sequence_no,
                work_category == SessionReplyWorkCategory.FOREGROUND.value,
                SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
                SessionReplyWorkItem.status != SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def claim_next(
        self,
        db: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: int,
        scheduled_profile_limits: dict[int, int] | None = None,
        independent_only: bool | None = None,
        agent_work_max_concurrency: int = 4,
        subagent_profile_limits: dict[int, int] | None = None,
        exclude_work_ids: set[int] | None = None,
    ) -> SessionReplyWorkItem | None:
        now = int(time.time())
        earlier_work = SessionReplyWorkItem.__table__.alias("earlier_session_reply_work")
        active_running = SessionReplyWorkItem.__table__.alias("active_running_session_reply_work")
        work_category = _work_category_expression(SessionReplyWorkItem.__table__)
        earlier_category = _work_category_expression(earlier_work)
        running_category = _work_category_expression(active_running)
        scheduled_limits = scheduled_profile_limits
        if scheduled_limits is None:
            scheduled_limits = {}
        subagent_limits = subagent_profile_limits
        if subagent_limits is None:
            subagent_limits = {}

        scheduled_limit_cases: list[tuple[Any, int]] = []
        for profile_id, limit in scheduled_limits.items():
            scheduled_limit_cases.append((SessionReplyWorkItem.profile_id == profile_id, limit))
        subagent_limit_cases: list[tuple[Any, int]] = []
        for profile_id, limit in subagent_limits.items():
            subagent_limit_cases.append((SessionReplyWorkItem.profile_id == profile_id, limit))
        scheduled_profile_limit: Any = 4
        if scheduled_limit_cases:
            scheduled_profile_limit = case(*scheduled_limit_cases, else_=4)
        subagent_profile_limit: Any = 4
        if subagent_limit_cases:
            subagent_profile_limit = case(*subagent_limit_cases, else_=4)

        independent_running_count = (
            select(func.count(active_running.c.id))
            .where(
                running_category != SessionReplyWorkCategory.FOREGROUND.value,
                active_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                active_running.c.lock_until > now,
            )
            .scalar_subquery()
        )
        scheduled_running_count = (
            select(func.count(active_running.c.id))
            .where(
                active_running.c.profile_id == SessionReplyWorkItem.profile_id,
                running_category == SessionReplyWorkCategory.SCHEDULED_TASK.value,
                active_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                active_running.c.lock_until > now,
            )
            .scalar_subquery()
        )
        subagent_running_count = (
            select(func.count(active_running.c.id))
            .where(
                active_running.c.profile_id == SessionReplyWorkItem.profile_id,
                running_category == SessionReplyWorkCategory.SUBAGENT.value,
                active_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                active_running.c.lock_until > now,
            )
            .scalar_subquery()
        )
        scheduled_profile_capacity_available = or_(
            work_category != SessionReplyWorkCategory.SCHEDULED_TASK.value,
            scheduled_running_count < scheduled_profile_limit,
        )
        subagent_profile_capacity_available = or_(
            work_category != SessionReplyWorkCategory.SUBAGENT.value,
            subagent_running_count < subagent_profile_limit,
        )

        candidate_conditions = [
            SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            SessionReplyWorkItem.available_at <= now,
            or_(
                work_category == SessionReplyWorkCategory.FOREGROUND.value,
                independent_running_count < agent_work_max_concurrency,
            ),
            scheduled_profile_capacity_available,
            subagent_profile_capacity_available,
        ]
        if independent_only is False:
            candidate_conditions.append(work_category == SessionReplyWorkCategory.FOREGROUND.value)
        elif independent_only is True:
            candidate_conditions.append(work_category != SessionReplyWorkCategory.FOREGROUND.value)
        if exclude_work_ids:
            candidate_conditions.append(SessionReplyWorkItem.id.not_in(exclude_work_ids))
        candidate_conditions.extend(
            [
                or_(
                    work_category != SessionReplyWorkCategory.FOREGROUND.value,
                    ~exists(
                        select(1).where(
                            earlier_work.c.session_id == SessionReplyWorkItem.session_id,
                            earlier_work.c.sequence_no < SessionReplyWorkItem.sequence_no,
                            earlier_category == SessionReplyWorkCategory.FOREGROUND.value,
                            earlier_work.c.status.not_in(_TERMINAL_STATUS_VALUES),
                            earlier_work.c.status != SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
                        )
                    ),
                ),
                or_(
                    work_category != SessionReplyWorkCategory.FOREGROUND.value,
                    ~exists(
                        select(1).where(
                            active_running.c.session_id == SessionReplyWorkItem.session_id,
                            running_category == SessionReplyWorkCategory.FOREGROUND.value,
                            active_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                            active_running.c.lock_until > now,
                        )
                    ),
                ),
            ]
        )
        candidate = select(SessionReplyWorkItem.id).where(*candidate_conditions).order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id).limit(1).scalar_subquery()
        statement = (
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == candidate,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .values(
                status=SessionReplyWorkStatus.RUNNING,
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                attempt_count=SessionReplyWorkItem.attempt_count + 1,
                updated_at=get_local_time(),
            )
            .returning(SessionReplyWorkItem)
        )
        result = await db.execute(statement)
        claimed = result.scalars().first()
        await db.commit()
        return claimed

    async def get_active_claims(
        self,
        db: AsyncSession,
        claims: dict[int, str],
    ) -> set[tuple[int, str]]:
        if not claims:
            return set()
        claim_conditions = [
            and_(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            for work_id, worker_id in claims.items()
        ]
        result = await db.execute(
            select(SessionReplyWorkItem.id, SessionReplyWorkItem.locked_by).where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                or_(*claim_conditions),
            )
        )
        return {(work_id, worker_id) for work_id, worker_id in result.all() if worker_id is not None}

    async def renew_active_claims(
        self,
        db: AsyncSession,
        *,
        claims: dict[int, str],
        lease_seconds: int,
    ) -> set[tuple[int, str]]:
        active_claims = await self.get_active_claims(db, claims)
        if active_claims:
            await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    or_(
                        *[
                            and_(
                                SessionReplyWorkItem.id == work_id,
                                SessionReplyWorkItem.locked_by == worker_id,
                            )
                            for work_id, worker_id in active_claims
                        ]
                    ),
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                )
                .values(
                    lock_until=int(time.time()) + lease_seconds,
                    updated_at=get_local_time(),
                )
            )
            await db.commit()
        return active_claims

    async def list_ready_agent_profile_ids(self, db: AsyncSession) -> set[int]:
        result = await db.execute(
            select(SessionReplyWorkItem.profile_id)
            .where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                _work_category_expression(SessionReplyWorkItem.__table__) != SessionReplyWorkCategory.FOREGROUND.value,
            )
            .distinct()
        )
        return set(result.scalars().all())

    async def renew_lease(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(lock_until=int(time.time()) + lease_seconds, updated_at=get_local_time())
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def release_for_retry(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        error: str,
        delay_seconds: int,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=int(time.time()) + delay_seconds,
                error=error,
                updated_at=get_local_time(),
            )
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        work_conditions = [SessionReplyWorkItem.session_id == session_id]
        if not is_admin:
            work_conditions.append(SessionReplyWorkItem.uid == uid)

        work_ids = select(SessionReplyWorkItem.id).where(*work_conditions)
        await db.execute(delete(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id.in_(work_ids)).execution_options(synchronize_session=False))
        result = await db.execute(delete(SessionReplyWorkItem).where(*work_conditions).execution_options(synchronize_session=False))
        await db.execute(delete(SessionReplySequence).where(SessionReplySequence.session_id == session_id).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def _cleanup_excess_work(
        self,
        db: AsyncSession,
        *,
        retention_limit: int,
        batch_size: int,
    ) -> tuple[int, int, int]:
        if batch_size <= 0:
            return 0, 0, 0

        dialect_name = db.get_bind().dialect.name
        target_work = SessionReplyWorkItem.__table__.alias("cleanup_target_work")
        merged_into_active = (
            select(1)
            .select_from(target_work)
            .where(
                target_work.c.id == SessionReplyWorkItem.merged_into_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.MERGED.value,
                target_work.c.uid == SessionReplyWorkItem.uid,
                target_work.c.session_id == SessionReplyWorkItem.session_id,
                target_work.c.status.in_([status.value for status in SESSION_REPLY_ACTIVE_STATUSES]),
            )
            .correlate(SessionReplyWorkItem)
        )
        protected = reply_work_is_protected(SessionReplyWorkItem, dialect_name=dialect_name)
        base_conditions = [
            SessionReplyWorkItem.status.in_(_TERMINAL_STATUS_VALUES),
            SessionReplyWorkItem.subagent_id.is_(None),
            ~protected,
        ]
        candidate_conditions = [*base_conditions, ~exists(merged_into_active)]
        boundary_condition: Any | None = None

        if retention_limit > 0:
            boundary_result = await db.execute(select(SessionReplyWorkItem.updated_at, SessionReplyWorkItem.id).where(*candidate_conditions).order_by(SessionReplyWorkItem.updated_at.desc(), SessionReplyWorkItem.id.desc()).offset(retention_limit - 1).limit(1))
            boundary = boundary_result.first()
            if boundary is None:
                return 0, 0, 0
            boundary_updated_at, boundary_id = boundary
            boundary_condition = or_(
                SessionReplyWorkItem.updated_at < boundary_updated_at,
                and_(
                    SessionReplyWorkItem.updated_at == boundary_updated_at,
                    SessionReplyWorkItem.id < boundary_id,
                ),
            )
            candidate_conditions.append(boundary_condition)

        candidate_result = await db.execute(select(SessionReplyWorkItem.id).where(*candidate_conditions).order_by(SessionReplyWorkItem.updated_at.asc(), SessionReplyWorkItem.id.asc()).limit(batch_size).with_for_update())
        candidate_ids = [work_id for work_id in candidate_result.scalars().all() if work_id is not None]
        if not candidate_ids:
            return 0, 0, 0

        if dialect_name == "sqlite":
            locked_result = await db.execute(update(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(candidate_ids), *candidate_conditions).values(updated_at=SessionReplyWorkItem.updated_at).returning(SessionReplyWorkItem.id).execution_options(synchronize_session=False))
            locked_id_set = {work_id for work_id in locked_result.scalars().all() if work_id is not None}
            locked_ids = [work_id for work_id in candidate_ids if work_id in locked_id_set]
        else:
            locked_ids = candidate_ids
        if not locked_ids:
            return 0, 0, 0

        event_result = await db.execute(
            select(SessionReplyStreamEvent.id)
            .join(SessionReplyWorkItem, SessionReplyWorkItem.id == SessionReplyStreamEvent.work_id)
            .where(SessionReplyStreamEvent.work_id.in_(locked_ids))
            .order_by(
                SessionReplyWorkItem.updated_at.asc(),
                SessionReplyWorkItem.id.asc(),
                SessionReplyStreamEvent.id.asc(),
            )
            .limit(batch_size)
            .with_for_update()
        )
        event_ids = [event_id for event_id in event_result.scalars().all() if event_id is not None]
        stream_event_count = 0
        if event_ids:
            deleted_event_result = await db.execute(delete(SessionReplyStreamEvent).where(SessionReplyStreamEvent.id.in_(event_ids)).execution_options(synchronize_session=False))
            stream_event_count = deleted_event_result.rowcount or 0

        remaining = batch_size - stream_event_count
        if remaining <= 0:
            return 0, stream_event_count, 0

        private_message_result = await db.execute(
            select(Message.id)
            .join(
                SessionReplyWorkItem,
                and_(
                    SessionReplyWorkItem.id == Message.work_id,
                    SessionReplyWorkItem.uid == Message.uid,
                    SessionReplyWorkItem.session_id == Message.session_id,
                    SessionReplyWorkItem.profile_id == Message.profile_id,
                ),
            )
            .where(
                SessionReplyWorkItem.id.in_(locked_ids),
                Message.work_id.is_not(None),
            )
            .order_by(
                SessionReplyWorkItem.updated_at.asc(),
                SessionReplyWorkItem.id.asc(),
                Message.id.asc(),
            )
            .limit(remaining)
            .with_for_update()
        )
        private_message_ids = [message_id for message_id in private_message_result.scalars().all() if message_id is not None]
        private_message_count = 0
        if private_message_ids:
            deleted_message_result = await db.execute(
                delete(Message)
                .where(
                    Message.id.in_(private_message_ids),
                    Message.work_id.is_not(None),
                )
                .execution_options(synchronize_session=False)
            )
            private_message_count = deleted_message_result.rowcount or 0

        remaining -= private_message_count
        if remaining <= 0:
            return 0, stream_event_count, private_message_count

        work_result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.id.in_(locked_ids),
                ~exists(select(1).select_from(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id)),
                ~exists(
                    select(1)
                    .select_from(Message)
                    .where(
                        Message.work_id == SessionReplyWorkItem.id,
                        Message.uid == SessionReplyWorkItem.uid,
                        Message.session_id == SessionReplyWorkItem.session_id,
                        Message.profile_id == SessionReplyWorkItem.profile_id,
                    )
                ),
            )
            .order_by(SessionReplyWorkItem.updated_at.asc(), SessionReplyWorkItem.id.asc())
            .limit(remaining)
            .with_for_update()
        )
        work_ids = [work_id for work_id in work_result.scalars().all() if work_id is not None]
        if not work_ids:
            return 0, stream_event_count, private_message_count

        deleted_work_result = await db.execute(delete(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(work_ids)).execution_options(synchronize_session=False))
        return deleted_work_result.rowcount or 0, stream_event_count, private_message_count

    async def _cleanup_orphan_rows(
        self,
        db: AsyncSession,
        *,
        model: Any,
        primary_key: Any,
        conditions: list[Any],
        limit: int,
    ) -> int:
        orphan_result = await db.execute(select(primary_key).where(*conditions).order_by(primary_key).limit(limit).with_for_update())
        orphan_ids = [value for value in orphan_result.scalars().all() if value is not None]
        if not orphan_ids:
            return 0

        deleted_result = await db.execute(delete(model).where(primary_key.in_(orphan_ids), *conditions).execution_options(synchronize_session=False))
        return deleted_result.rowcount or 0

    async def cleanup_terminal_items(
        self,
        db: AsyncSession,
        *,
        retention_limit: int = SESSION_REPLY_WORK_RETENTION_LIMIT,
        batch_size: int = SESSION_REPLY_CLEANUP_BATCH_SIZE,
    ) -> SessionReplyCleanupResult:
        if batch_size <= 0:
            return SessionReplyCleanupResult()

        try:
            work_item_count, stream_event_count, private_message_count = await self._cleanup_excess_work(
                db,
                retention_limit=retention_limit,
                batch_size=batch_size,
            )
            remaining = max(0, batch_size - work_item_count - stream_event_count - private_message_count)
            sequence_count = 0
            checkpoint_message_count = 0

            if remaining > 0:
                deleted = await self._cleanup_orphan_rows(
                    db,
                    model=SessionReplyStreamEvent,
                    primary_key=SessionReplyStreamEvent.id,
                    conditions=[~exists(select(1).select_from(SessionReplyWorkItem).where(SessionReplyWorkItem.id == SessionReplyStreamEvent.work_id))],
                    limit=remaining,
                )
                stream_event_count += deleted
                remaining -= deleted

            if remaining > 0:
                deleted = await self._cleanup_orphan_rows(
                    db,
                    model=SessionReplySequence,
                    primary_key=SessionReplySequence.session_id,
                    conditions=[
                        ~exists(select(1).select_from(ChatSession).where(ChatSession.session_id == SessionReplySequence.session_id)),
                        ~exists(select(1).select_from(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == SessionReplySequence.session_id)),
                    ],
                    limit=remaining,
                )
                sequence_count += deleted
                remaining -= deleted

            if remaining > 0:
                checkpoint_message_count = await session_reply_checkpoint_message_crud.cleanup_unreferenced(db, limit=remaining)
                remaining -= checkpoint_message_count

            await db.commit()
        except BaseException:
            await db.rollback()
            raise

        return SessionReplyCleanupResult(
            work_items=work_item_count,
            stream_events=stream_event_count,
            private_messages=private_message_count,
            sequences=sequence_count,
            checkpoint_messages=checkpoint_message_count,
        )

    async def cancel_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
        interactive_only: bool = False,
    ) -> int:
        conditions = [
            SessionReplyWorkItem.session_id == session_id,
            SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
        ]
        if not is_admin:
            conditions.append(SessionReplyWorkItem.uid == uid)
        if interactive_only:
            conditions.append(
                SessionReplyWorkItem.work_type.in_(
                    [
                        SessionReplyWorkType.FOREGROUND_REPLY,
                        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                    ]
                )
            )
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(*conditions)
            .values(
                status=SessionReplyWorkStatus.CANCELLED,
                locked_by=None,
                lock_until=None,
                updated_at=get_local_time(),
            )
        )
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def cancel_agent_work_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        commit: bool = False,
    ) -> int:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.profile_id == profile_id,
                SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            )
            .values(
                status=SessionReplyWorkStatus.CANCELLED,
                locked_by=None,
                lock_until=None,
                updated_at=get_local_time(),
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return result.rowcount or 0

    async def cancel_background_tool_summaries(
        self,
        db: AsyncSession,
        *,
        error: str,
        before: datetime | None = None,
        commit: bool = True,
    ) -> int:
        snapshot_conditions = [
            SessionReplyWorkItem.work_type.in_([SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.BACKGROUND_TASK_RESUME]),
            SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
        ]
        if before is not None:
            snapshot_conditions.append(SessionReplyWorkItem.created_at <= before)
        snapshot_result = await db.execute(select(SessionReplyWorkItem.id).where(*snapshot_conditions).order_by(SessionReplyWorkItem.id.asc()))
        snapshot_ids = [work_id for work_id in snapshot_result.scalars().all() if work_id is not None]
        cancelled_count = 0
        if snapshot_ids:
            result = await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id.in_(snapshot_ids),
                    SessionReplyWorkItem.work_type.in_([SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.BACKGROUND_TASK_RESUME]),
                    SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
                )
                .values(
                    status=SessionReplyWorkStatus.CANCELLED,
                    locked_by=None,
                    lock_until=None,
                    error=error,
                    updated_at=get_local_time(),
                )
            )
            cancelled_count = result.rowcount or 0
        if commit:
            await db.commit()
        return cancelled_count

    async def list_startup_cancellation_work_items(
        self,
        db: AsyncSession,
        *,
        before: datetime | None = None,
    ) -> list[SessionReplyWorkItem]:
        snapshot_conditions = [
            or_(
                and_(
                    SessionReplyWorkItem.work_type.in_([SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.BACKGROUND_TASK_RESUME]),
                    SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
                ),
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
        ]
        if before is not None:
            snapshot_conditions.append(SessionReplyWorkItem.created_at <= before)
        result = await db.execute(select(SessionReplyWorkItem).where(*snapshot_conditions).order_by(SessionReplyWorkItem.id.asc()).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def recover_expired(self, db: AsyncSession) -> tuple[int, list[tuple[int, str, str]]]:
        now = int(time.time())
        expired_conditions = [
            SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
            or_(SessionReplyWorkItem.lock_until.is_(None), SessionReplyWorkItem.lock_until <= now),
        ]
        expired_result = await db.execute(
            select(SessionReplyWorkItem).where(
                *expired_conditions,
                SessionReplyWorkItem.locked_by.is_not(None),
            )
        )
        expired_work = list(expired_result.scalars().all())
        expired_work_ids = [work.id for work in expired_work if work.id is not None]
        streamed_work_ids: set[int] = set()
        if expired_work_ids:
            stream_result = await db.execute(
                select(SessionReplyStreamEvent.work_id).where(
                    SessionReplyStreamEvent.work_id.in_(expired_work_ids),
                    func.coalesce(SessionReplyStreamEvent.event["type"].as_string(), "") != "work_suspended",
                )
            )
            streamed_work_ids = {work_id for work_id in stream_result.scalars().all() if work_id is not None}

        terminal_claims: list[tuple[int, str, str]] = []
        recovered_count = 0
        for work in expired_work:
            if work.id is None or work.locked_by is None:
                continue
            state = work.execution_state if isinstance(work.execution_state, dict) else {}
            active_audit_execution = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in state
            stream_started = work.id in streamed_work_ids
            terminal_error = None
            if active_audit_execution:
                terminal_error = t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN)
            elif work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
                terminal_error = "Confirmed tool execution was interrupted; result unknown and automatic retry is forbidden"
            else:
                if state.get("subagent_board_review_pending") is True and work.work_category == SessionReplyWorkCategory.FOREGROUND:
                    checkpoint = await self._get_checkpoint_payload(db, work.id)
                    if isinstance(checkpoint, dict) and checkpoint.get("yield_to_user_pending") is True:
                        yielded = await self.yield_board_review(
                            db,
                            work_id=work.id,
                            worker_id=work.locked_by,
                            expired_at=now,
                            commit=False,
                        )
                        if yielded:
                            recovered_count += 1
                        continue
                if stream_started:
                    terminal_error = "Stream interrupted after partial response"
                elif work.attempt_count >= work.max_attempts:
                    terminal_error = "Maximum retry attempts reached after worker interruption"

            if terminal_error is not None:
                terminal_claims.append((work.id, work.locked_by, terminal_error))
                continue

            retry_result = await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id == work.id,
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                    SessionReplyWorkItem.locked_by == work.locked_by,
                    or_(SessionReplyWorkItem.lock_until.is_(None), SessionReplyWorkItem.lock_until <= now),
                    SessionReplyWorkItem.attempt_count < SessionReplyWorkItem.max_attempts,
                )
                .values(
                    status=SessionReplyWorkStatus.READY_FOR_LLM,
                    locked_by=None,
                    lock_until=None,
                    available_at=now,
                    updated_at=get_local_time(),
                )
            )
            recovered_count += retry_result.rowcount or 0
        await db.commit()
        return recovered_count + len(terminal_claims), terminal_claims

    async def update_claimed(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        values: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(**values, updated_at=get_local_time())
        )
        claimed = (result.rowcount or 0) == 1
        if claimed and checkpoint is not None:
            # 工作行条件更新与快照写入须在同一事务内完成，以串行化多执行者更新。
            await self.save_checkpoint(db, work_id, checkpoint)
        if commit:
            await db.commit()
        return claimed

    async def merge_ready_foreground(
        self,
        db: AsyncSession,
        *,
        work_ids: list[int],
        merged_into_id: int,
    ) -> list[int]:
        if not work_ids:
            return []
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id.in_(work_ids),
                SessionReplyWorkItem.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .values(
                status=SessionReplyWorkStatus.MERGED,
                merged_into_id=merged_into_id,
                locked_by=None,
                lock_until=None,
                updated_at=get_local_time(),
            )
            .returning(SessionReplyWorkItem.id)
        )
        return [work_id for work_id in result.scalars().all() if work_id is not None]

    async def mark_terminal(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        status: SessionReplyWorkStatus,
        result_message_id: int | None = None,
        error: str | None = None,
        event_sent: bool | None = None,
        commit: bool = True,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "result_message_id": result_message_id,
            "error": error,
            "locked_by": None,
            "lock_until": None,
        }
        if event_sent is not None:
            values["event_sent"] = event_sent
        return await self.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values=values,
            commit=commit,
        )

    async def resolve_merged_target(self, db: AsyncSession, work_id: int) -> SessionReplyWorkItem | None:
        current = await self.get(db, work_id)
        visited: set[int] = set()
        while current and current.status == SessionReplyWorkStatus.MERGED and current.merged_into_id:
            if current.id is None or current.id in visited:
                return None
            visited.add(current.id)
            current = await self.get(db, current.merged_into_id)
        return current

    async def list_contiguous_foreground(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
    ) -> list[SessionReplyWorkItem]:
        work_category = _work_category_expression(SessionReplyWorkItem.__table__)
        blocking_sequence_result = await db.execute(
            select(SessionReplyWorkItem.sequence_no)
            .where(
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.sequence_no > work.sequence_no,
                SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
                SessionReplyWorkItem.status != SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                work_category == SessionReplyWorkCategory.FOREGROUND.value,
                or_(
                    SessionReplyWorkItem.work_type != SessionReplyWorkType.FOREGROUND_REPLY,
                    SessionReplyWorkItem.profile_id != work.profile_id,
                    _is_subagent_board_review(),
                ),
            )
            .order_by(SessionReplyWorkItem.sequence_no)
            .limit(1)
        )
        blocking_sequence = blocking_sequence_result.scalar()
        conditions = [
            SessionReplyWorkItem.session_id == work.session_id,
            SessionReplyWorkItem.sequence_no >= work.sequence_no,
            SessionReplyWorkItem.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
            SessionReplyWorkItem.profile_id == work.profile_id,
            SessionReplyWorkItem.status.in_(
                [
                    SessionReplyWorkStatus.READY_FOR_LLM,
                    SessionReplyWorkStatus.RUNNING,
                ]
            ),
        ]
        if blocking_sequence is not None:
            conditions.append(SessionReplyWorkItem.sequence_no < blocking_sequence)
        result = await db.execute(select(SessionReplyWorkItem).where(and_(*conditions)).order_by(SessionReplyWorkItem.sequence_no))
        contiguous: list[SessionReplyWorkItem] = []
        for item in result.scalars().all():
            if item.id != work.id and item.input_message_ids:
                break
            contiguous.append(item)
        return contiguous


session_reply_work_item_crud = CRUDSessionReplyWorkItem()


# 校验队列回复事务仍持有工作项租约。
async def ensure_session_reply_work_claim(db: AsyncSession, *, commit: bool = False) -> None:
    claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    if not claim:
        return

    work_id, worker_id = claim
    claimed = await session_reply_work_item_crud.update_claimed(
        db,
        work_id=work_id,
        worker_id=worker_id,
        values={},
        commit=False,
    )
    if not claimed:
        await db.rollback()
        raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))
    if commit:
        await db.commit()
