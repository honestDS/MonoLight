import time
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, exists, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import SESSION_REPLY_CLEANUP_BATCH_SIZE, SUBAGENT_IDLE_RETENTION_HOURS
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.time import get_local_time
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork


class CRUDSubagent:
    async def cleanup_idle(
        self,
        db: AsyncSession,
        *,
        retention_hours: int = SUBAGENT_IDLE_RETENTION_HOURS,
        batch_size: int = SESSION_REPLY_CLEANUP_BATCH_SIZE,
    ) -> int:
        if batch_size <= 0:
            return 0

        cutoff = get_local_time() - timedelta(hours=retention_hours)
        active_work = (
            select(1)
            .select_from(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.subagent_id == Subagent.id,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            )
            .correlate(Subagent)
        )
        unfinished_round = (
            select(1)
            .select_from(SubagentWork)
            .where(
                SubagentWork.subagent_id == Subagent.id,
                or_(SubagentWork.result.is_(None), SubagentWork.waiting_confirmation.is_(True)),
            )
            .correlate(Subagent)
        )
        undelivered_call = (
            select(1)
            .select_from(SubagentToolCall)
            .where(
                SubagentToolCall.subagent_id == Subagent.id,
                SubagentToolCall.delivered.is_(False),
            )
            .correlate(Subagent)
        )
        eligible_conditions = (
            Subagent.last_called_at < cutoff,
            Subagent.active_child_work_id.is_(None),
            ~exists(active_work),
            ~exists(unfinished_round),
            ~exists(undelivered_call),
        )

        deleted_count = 0
        try:
            result = await db.execute(select(Subagent.id).where(*eligible_conditions).order_by(Subagent.last_called_at.asc(), Subagent.id.asc()).limit(batch_size))
            candidate_ids = [subagent_id for subagent_id in result.scalars().all() if subagent_id is not None]

            for candidate_id in candidate_ids:
                update_result = await db.execute(update(Subagent).where(Subagent.id == candidate_id, *eligible_conditions).values(last_called_at=Subagent.last_called_at).execution_options(synchronize_session=False))
                if update_result.rowcount != 1:
                    continue

                subagent_result = await db.execute(select(Subagent.uid, Subagent.session_id, Subagent.profile_id, Subagent.id).where(Subagent.id == candidate_id))
                subagent = subagent_result.one_or_none()
                if subagent is None:
                    continue

                work_result = await db.execute(
                    select(SessionReplyWorkItem.id).where(
                        SessionReplyWorkItem.subagent_id == subagent.id,
                        SessionReplyWorkItem.uid == subagent.uid,
                        SessionReplyWorkItem.session_id == subagent.session_id,
                        SessionReplyWorkItem.profile_id == subagent.profile_id,
                    )
                )
                work_ids = [work_id for work_id in work_result.scalars().all() if work_id is not None]

                checkpoint_message_ids: list[int] = []
                if work_ids:
                    reference_result = await db.execute(select(SessionReplyCheckpointMessageReference.message_id).where(SessionReplyCheckpointMessageReference.work_id.in_(work_ids)).distinct().order_by(SessionReplyCheckpointMessageReference.message_id.asc()))
                    checkpoint_message_ids = list(reference_result.scalars().all())

                    await message_crud.remove_by_work_ids(
                        db,
                        work_ids=work_ids,
                        uid=subagent.uid,
                        session_id=subagent.session_id,
                        profile_id=subagent.profile_id,
                    )
                    await db.execute(
                        delete(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.id.in_(work_ids),
                            SessionReplyWorkItem.subagent_id == subagent.id,
                            SessionReplyWorkItem.uid == subagent.uid,
                            SessionReplyWorkItem.session_id == subagent.session_id,
                            SessionReplyWorkItem.profile_id == subagent.profile_id,
                        )
                        .execution_options(synchronize_session=False)
                    )

                delete_result = await db.execute(delete(Subagent).where(Subagent.id == subagent.id).execution_options(synchronize_session=False))

                if checkpoint_message_ids:
                    await db.execute(
                        delete(SessionReplyCheckpointMessage)
                        .where(
                            SessionReplyCheckpointMessage.id.in_(checkpoint_message_ids),
                            ~exists(select(1).select_from(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id).correlate(SessionReplyCheckpointMessage)),
                        )
                        .execution_options(synchronize_session=False)
                    )

                if delete_result.rowcount == 1:
                    deleted_count += 1

            await db.commit()
            return deleted_count
        except BaseException:
            await db.rollback()
            raise

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        name: str,
    ) -> Subagent:
        subagent = Subagent(
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            name=name,
        )
        db.add(subagent)
        await db.flush()
        return subagent

    async def get_scoped(
        self,
        db: AsyncSession,
        *,
        subagent_id: str,
        uid: str,
        session_id: str,
        profile_id: int,
        lock: bool = False,
        touch: bool = False,
    ) -> Subagent | None:
        conditions = (
            Subagent.id == subagent_id,
            Subagent.uid == uid,
            Subagent.session_id == session_id,
            Subagent.profile_id == profile_id,
        )
        if lock or touch:
            last_called_at = get_local_time() if touch else Subagent.last_called_at
            await db.execute(update(Subagent).where(*conditions).values(last_called_at=last_called_at))

        statement = select(Subagent).where(*conditions).execution_options(populate_existing=True)
        if lock:
            statement = statement.with_for_update()
        result = await db.execute(statement)
        return result.scalars().first()

    async def list_active_scoped(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> list[tuple[Subagent, SubagentWork]]:
        result = await db.execute(
            select(Subagent, SubagentWork)
            .join(
                SubagentWork,
                (SubagentWork.subagent_id == Subagent.id) & (SubagentWork.child_work_id == Subagent.active_child_work_id),
            )
            .where(
                Subagent.uid == uid,
                Subagent.session_id == session_id,
                Subagent.profile_id == profile_id,
                SubagentWork.result.is_(None),
            )
            .order_by(Subagent.created_at.asc(), Subagent.id.asc())
            .execution_options(populate_existing=True)
        )

        active_rows: list[tuple[Subagent, SubagentWork]] = []
        for subagent, work in result.all():
            active_rows.append((subagent, work))
        return active_rows

    async def lock_parent_for_delivery(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        uid: str,
        session_id: str,
        profile_id: int,
        identity: str,
    ) -> SessionReplyWorkItem | None:
        statement = (
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
                SessionReplyWorkItem.dedupe_key == identity,
                SessionReplyWorkItem.status.in_(
                    (
                        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                        SessionReplyWorkStatus.READY_FOR_LLM,
                        SessionReplyWorkStatus.SUCCEEDED,
                    )
                ),
                SessionReplyWorkItem.subagent_id.is_(None),
            )
            .values(updated_at=get_local_time())
            .returning(SessionReplyWorkItem)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await db.execute(statement)
        return result.scalars().first()

    async def get_round(self, db: AsyncSession, *, child_work_id: int, subagent_id: str) -> SubagentWork | None:
        result = await db.execute(
            select(SubagentWork)
            .where(
                SubagentWork.child_work_id == child_work_id,
                SubagentWork.subagent_id == subagent_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def get_round_for_work(self, db: AsyncSession, work: SessionReplyWorkItem) -> SubagentWork | None:
        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        child_work_id = execution_state.get("child_work_id", work.id)
        result = await db.execute(
            select(SubagentWork)
            .where(
                SubagentWork.subagent_id == work.subagent_id,
                SubagentWork.child_work_id == child_work_id,
                SubagentWork.current_work_id == work.id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def list_pending_messages(self, db: AsyncSession, *, work: SessionReplyWorkItem) -> list[Message]:
        result = await db.execute(
            select(Message)
            .where(
                Message.work_id == work.id,
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.profile_id == work.profile_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed.is_(False),
            )
            .order_by(Message.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def requeue_for_messages(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        worker_id: str,
        execution_state: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        message_id: int | None,
    ) -> bool:
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "status": SessionReplyWorkStatus.READY_FOR_LLM,
                "locked_by": None,
                "lock_until": None,
                "result_message_id": None,
                "execution_state": execution_state,
                "attempt_count": 0,
                "available_at": int(time.time()),
                "error": None,
            },
            checkpoint=checkpoint,
            commit=False,
        )
        if updated and message_id is not None:
            await db.execute(
                update(Message)
                .where(
                    Message.id == message_id,
                    Message.work_id == work.id,
                    Message.uid == work.uid,
                    Message.session_id == work.session_id,
                    Message.profile_id == work.profile_id,
                )
                .values(dedupe_key=None)
            )
        return updated

    async def cancel_work(self, db: AsyncSession, *, work: SessionReplyWorkItem, error: str) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.profile_id == work.profile_id,
                SessionReplyWorkItem.subagent_id == work.subagent_id,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            )
            .values(
                status=SessionReplyWorkStatus.CANCELLED,
                locked_by=None,
                lock_until=None,
                error=error,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def acknowledge_messages(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        message_ids: list[int],
    ) -> None:
        await db.execute(
            update(Message)
            .where(
                Message.id.in_(message_ids),
                Message.work_id == work.id,
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.profile_id == work.profile_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed.is_(False),
            )
            .values(is_processed=True)
        )

    async def create_round(self, db: AsyncSession, *, subagent_id: str, child_work_id: int) -> SubagentWork:
        round_row = SubagentWork(
            subagent_id=subagent_id,
            child_work_id=child_work_id,
            current_work_id=child_work_id,
        )
        db.add(round_row)
        await db.flush()
        return round_row

    async def get_call(self, db: AsyncSession, *, parent_work_id: int, tool_call_id: str) -> SubagentToolCall | None:
        result = await db.execute(
            select(SubagentToolCall)
            .where(
                SubagentToolCall.parent_work_id == parent_work_id,
                SubagentToolCall.tool_call_id == tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def create_call(
        self,
        db: AsyncSession,
        *,
        subagent_id: str,
        child_work_id: int,
        parent_work_id: int,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> SubagentToolCall:
        call = SubagentToolCall(
            subagent_id=subagent_id,
            child_work_id=child_work_id,
            parent_work_id=parent_work_id,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )
        db.add(call)
        await db.flush()
        return call

    async def list_calls(
        self,
        db: AsyncSession,
        *,
        parent_work_id: int,
        undelivered_only: bool = True,
    ) -> list[SubagentToolCall]:
        conditions = [SubagentToolCall.parent_work_id == parent_work_id]
        if undelivered_only:
            conditions.append(SubagentToolCall.delivered.is_(False))
        result = await db.execute(select(SubagentToolCall).where(*conditions).order_by(SubagentToolCall.id.asc()).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def list_unfinished_rounds(
        self,
        db: AsyncSession,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[SubagentWork]:
        result = await db.execute(
            select(SubagentWork)
            .where(
                SubagentWork.result.is_(None),
                SubagentWork.child_work_id > after_id,
            )
            .order_by(SubagentWork.child_work_id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_parent_ids_for_child(self, db: AsyncSession, *, child_work_id: int) -> list[int]:
        result = await db.execute(
            select(SubagentToolCall.parent_work_id)
            .where(
                SubagentToolCall.child_work_id == child_work_id,
                SubagentToolCall.delivered.is_(False),
            )
            .distinct()
            .order_by(SubagentToolCall.parent_work_id.asc())
        )
        return list(result.scalars().all())

    async def list_pending_parent_ids(self, db: AsyncSession, *, after_id: int = 0, limit: int = 100) -> list[int]:
        result = await db.execute(
            select(SubagentToolCall.parent_work_id)
            .join(SubagentWork, SubagentWork.child_work_id == SubagentToolCall.child_work_id)
            .where(
                SubagentToolCall.delivered.is_(False),
                SubagentWork.result.is_not(None),
                SubagentToolCall.parent_work_id > after_id,
            )
            .distinct()
            .order_by(SubagentToolCall.parent_work_id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


subagent_crud = CRUDSubagent()
