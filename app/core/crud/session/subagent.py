import time
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, delete, exists, func, or_, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select
from sqlmodel import select

from app.core.constants import (
    SUBAGENT_BOARD_POST_DISPLAY_CHARS,
    SUBAGENT_BOARD_PREVIEW_CHARS,
    SUBAGENT_CLEANUP_BATCH_SIZE,
    SUBAGENT_RETENTION_LIMIT,
    SUBAGENT_ROUND_RETENTION_LIMIT,
)
from app.core.crud.session.reply_work_item import reply_work_is_protected, session_reply_work_item_crud
from app.core.crud.session.subagent_revision import bump_subagent_board_revisions
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.transactions import notify_after_commit
from app.core.utils.time import get_local_time
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork


def _board_round_completed():
    return or_(
        SubagentWork.result.is_not(None),
        SessionReplyWorkItem.status.in_((SessionReplyWorkStatus.FAILED, SessionReplyWorkStatus.CANCELLED)),
        and_(
            SessionReplyWorkItem.status == SessionReplyWorkStatus.SUCCEEDED,
            SubagentWork.waiting_confirmation.is_(False),
        ),
    )


def _protected_subagent_calls(*, dialect_name: str, child_work_id: Any | None = None) -> Select:
    parent_work = aliased(SessionReplyWorkItem)
    statement = (
        select(1)
        .select_from(SubagentToolCall)
        .join(parent_work, parent_work.id == SubagentToolCall.parent_work_id)
        .where(
            SubagentToolCall.subagent_id == Subagent.id,
            parent_work.uid == Subagent.uid,
            parent_work.session_id == Subagent.session_id,
            parent_work.profile_id == Subagent.profile_id,
            or_(
                SubagentToolCall.delivered.is_(False),
                reply_work_is_protected(parent_work, dialect_name=dialect_name),
            ),
        )
    )
    if child_work_id is not None:
        statement = statement.where(SubagentToolCall.child_work_id == child_work_id)
    return statement.correlate(Subagent, SubagentWork)


def _subagent_round_work_condition(work: Any, child_work_id: Any) -> Any:
    return or_(
        work.id == child_work_id,
        work.execution_state["child_work_id"].as_integer() == child_work_id,
    )


class CRUDSubagent:
    async def _remove_work_data(
        self,
        db: AsyncSession,
        *,
        work_conditions: list[Any],
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> None:
        work_ids = select(SessionReplyWorkItem.id).where(*work_conditions)
        await db.execute(
            delete(Message)
            .where(
                Message.work_id.in_(work_ids),
                Message.uid == uid,
                Message.session_id == session_id,
                Message.profile_id == profile_id,
            )
            .execution_options(synchronize_session=False)
        )

        checkpoint_message_ids = select(SessionReplyCheckpointMessageReference.message_id).where(SessionReplyCheckpointMessageReference.work_id.in_(work_ids)).distinct()
        other_work_reference = (
            select(1)
            .select_from(SessionReplyCheckpointMessageReference)
            .where(
                SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id,
                SessionReplyCheckpointMessageReference.work_id.not_in(work_ids),
            )
            .correlate(SessionReplyCheckpointMessage)
        )
        await db.execute(
            delete(SessionReplyCheckpointMessage)
            .where(
                SessionReplyCheckpointMessage.id.in_(checkpoint_message_ids),
                ~exists(other_work_reference),
            )
            .execution_options(synchronize_session=False)
        )
        await db.execute(delete(SessionReplyWorkItem).where(*work_conditions).execution_options(synchronize_session=False))

    async def cleanup_idle(
        self,
        db: AsyncSession,
        *,
        retention_limit: int = SUBAGENT_RETENTION_LIMIT,
        batch_size: int = SUBAGENT_CLEANUP_BATCH_SIZE,
    ) -> int:
        if batch_size <= 0:
            return 0

        dialect_name = db.get_bind().dialect.name
        protected_work = (
            select(1)
            .select_from(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.subagent_id == Subagent.id,
                SessionReplyWorkItem.uid == Subagent.uid,
                SessionReplyWorkItem.session_id == Subagent.session_id,
                SessionReplyWorkItem.profile_id == Subagent.profile_id,
                reply_work_is_protected(SessionReplyWorkItem, dialect_name=dialect_name),
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
        protected_subagent_calls = _protected_subagent_calls(dialect_name=dialect_name)
        eligible_conditions = (
            Subagent.active_child_work_id.is_(None),
            ~exists(protected_work),
            ~exists(unfinished_round),
            ~exists(protected_subagent_calls),
        )

        deleted_count = 0
        changed_session_ids: set[str] = set()
        try:
            count_result = await db.execute(select(func.count(Subagent.id)).select_from(Subagent).where(*eligible_conditions))
            eligible_count = int(count_result.scalar_one())
            excess_count = max(0, eligible_count - max(retention_limit, 0))
            candidate_limit = min(batch_size, excess_count)
            if candidate_limit <= 0:
                await db.commit()
                return 0

            candidate_result = await db.execute(
                select(
                    Subagent.id,
                    Subagent.uid,
                    Subagent.session_id,
                    Subagent.profile_id,
                    Subagent.last_called_at,
                )
                .where(*eligible_conditions)
                .order_by(Subagent.last_called_at.asc(), Subagent.id.asc())
                .limit(candidate_limit)
            )
            candidate_rows = candidate_result.all()

            for candidate_id, uid, session_id, profile_id, last_called_at in candidate_rows:
                update_result = await db.execute(
                    update(Subagent)
                    .where(
                        Subagent.id == candidate_id,
                        Subagent.uid == uid,
                        Subagent.session_id == session_id,
                        Subagent.profile_id == profile_id,
                        Subagent.last_called_at == last_called_at,
                        *eligible_conditions,
                    )
                    .values(last_called_at=Subagent.last_called_at)
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount != 1:
                    continue

                work_conditions = [
                    SessionReplyWorkItem.subagent_id == candidate_id,
                    SessionReplyWorkItem.uid == uid,
                    SessionReplyWorkItem.session_id == session_id,
                    SessionReplyWorkItem.profile_id == profile_id,
                ]
                await self._remove_work_data(
                    db,
                    work_conditions=work_conditions,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                )
                delete_result = await db.execute(
                    delete(Subagent)
                    .where(
                        Subagent.id == candidate_id,
                        Subagent.uid == uid,
                        Subagent.session_id == session_id,
                        Subagent.profile_id == profile_id,
                    )
                    .execution_options(synchronize_session=False)
                )

                if delete_result.rowcount == 1:
                    deleted_count += 1
                    changed_session_ids.add(session_id)

            await bump_subagent_board_revisions(db, changed_session_ids)
            await db.commit()
            return deleted_count
        except BaseException:
            await db.rollback()
            raise

    async def cleanup_old_rounds(
        self,
        db: AsyncSession,
        *,
        retention_limit: int = SUBAGENT_ROUND_RETENTION_LIMIT,
        batch_size: int = SUBAGENT_CLEANUP_BATCH_SIZE,
    ) -> int:
        if batch_size <= 0:
            return 0

        dialect_name = db.get_bind().dialect.name
        round_work = aliased(SessionReplyWorkItem)
        round_work_scope = (
            round_work.subagent_id == SubagentWork.subagent_id,
            round_work.uid == Subagent.uid,
            round_work.session_id == Subagent.session_id,
            round_work.profile_id == Subagent.profile_id,
        )
        round_work_ids = (
            select(round_work.id)
            .where(
                *round_work_scope,
                _subagent_round_work_condition(round_work, SubagentWork.child_work_id),
            )
            .correlate(Subagent, SubagentWork)
        )
        protected_round_work = (
            select(1)
            .select_from(round_work)
            .where(
                *round_work_scope,
                _subagent_round_work_condition(round_work, SubagentWork.child_work_id),
                reply_work_is_protected(round_work, dialect_name=dialect_name),
            )
            .correlate(Subagent, SubagentWork)
        )
        other_round_work = aliased(SessionReplyWorkItem)
        protected_source_message = (
            select(1)
            .select_from(Message)
            .join(
                SessionReplyCheckpointMessage,
                and_(
                    SessionReplyCheckpointMessage.payload["id"].as_integer() == Message.id,
                    SessionReplyCheckpointMessage.uid == Subagent.uid,
                    SessionReplyCheckpointMessage.session_id == Subagent.session_id,
                ),
            )
            .join(
                SessionReplyCheckpointMessageReference,
                SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id,
            )
            .join(other_round_work, other_round_work.id == SessionReplyCheckpointMessageReference.work_id)
            .where(
                Message.work_id.in_(round_work_ids),
                Message.uid == Subagent.uid,
                Message.session_id == Subagent.session_id,
                Message.profile_id == Subagent.profile_id,
                Message.role == MessageRole.ASSISTANT,
                # 旧 TOOL_CALL 源行在后续轮次恢复工具结果时仍可能被要求。
                Message.type == MessageType.TOOL_CALL,
                other_round_work.subagent_id == SubagentWork.subagent_id,
                other_round_work.uid == Subagent.uid,
                other_round_work.session_id == Subagent.session_id,
                other_round_work.profile_id == Subagent.profile_id,
                SessionReplyCheckpointMessageReference.work_id.not_in(round_work_ids),
            )
            .correlate(Subagent, SubagentWork)
        )
        protected_subagent_calls = _protected_subagent_calls(
            dialect_name=dialect_name,
            child_work_id=SubagentWork.child_work_id,
        )
        eligible_rounds = (
            select(
                SubagentWork.child_work_id,
                SubagentWork.subagent_id,
                Subagent.uid,
                Subagent.session_id,
                Subagent.profile_id,
                Subagent.last_called_at,
            )
            .select_from(SubagentWork)
            .join(Subagent, Subagent.id == SubagentWork.subagent_id)
            .where(
                or_(
                    Subagent.latest_child_work_id.is_(None),
                    Subagent.latest_child_work_id != SubagentWork.child_work_id,
                ),
                or_(
                    Subagent.active_child_work_id.is_(None),
                    Subagent.active_child_work_id != SubagentWork.child_work_id,
                ),
                SubagentWork.result.is_not(None),
                SubagentWork.waiting_confirmation.is_(False),
                ~exists(protected_subagent_calls),
                ~exists(protected_round_work),
                ~exists(protected_source_message),
            )
        )

        deleted_count = 0
        changed_session_ids: set[str] = set()
        try:
            count_result = await db.execute(select(func.count()).select_from(eligible_rounds.subquery()))
            eligible_count = int(count_result.scalar_one())
            excess_count = max(0, eligible_count - max(retention_limit, 0))
            candidate_limit = min(batch_size, excess_count)
            if candidate_limit <= 0:
                await db.commit()
                return 0

            candidate_result = await db.execute(eligible_rounds.order_by(SubagentWork.created_at.asc(), SubagentWork.child_work_id.asc()).limit(candidate_limit))
            candidate_rows = candidate_result.all()
            for child_work_id, subagent_id, uid, session_id, profile_id, last_called_at in candidate_rows:
                update_result = await db.execute(
                    update(Subagent)
                    .where(
                        Subagent.id == subagent_id,
                        Subagent.uid == uid,
                        Subagent.session_id == session_id,
                        Subagent.profile_id == profile_id,
                        Subagent.last_called_at == last_called_at,
                    )
                    .values(last_called_at=Subagent.last_called_at)
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount != 1:
                    continue

                recheck_result = await db.execute(
                    eligible_rounds.where(
                        Subagent.id == subagent_id,
                        Subagent.uid == uid,
                        Subagent.session_id == session_id,
                        Subagent.profile_id == profile_id,
                        SubagentWork.child_work_id == child_work_id,
                    ).limit(1)
                )
                if recheck_result.scalar_one_or_none() is None:
                    continue

                work_conditions = [
                    SessionReplyWorkItem.subagent_id == subagent_id,
                    SessionReplyWorkItem.uid == uid,
                    SessionReplyWorkItem.session_id == session_id,
                    SessionReplyWorkItem.profile_id == profile_id,
                    _subagent_round_work_condition(SessionReplyWorkItem, child_work_id),
                ]
                await self._remove_work_data(
                    db,
                    work_conditions=work_conditions,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                )
                deleted_count += 1
                changed_session_ids.add(session_id)

            await bump_subagent_board_revisions(db, changed_session_ids)
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

    def _board_scope(
        self,
        statement: Select,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        subagent_id: str | None = None,
    ) -> Select:
        statement = (
            statement.select_from(Subagent)
            .join(
                SubagentWork,
                (SubagentWork.child_work_id == Subagent.latest_child_work_id) & (SubagentWork.subagent_id == Subagent.id),
            )
            .join(SessionReplyWorkItem, SessionReplyWorkItem.id == SubagentWork.current_work_id)
            .where(
                Subagent.uid == uid,
                Subagent.session_id == session_id,
                Subagent.profile_id == profile_id,
                SessionReplyWorkItem.subagent_id == Subagent.id,
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
            )
        )
        if subagent_id is not None:
            statement = statement.where(Subagent.id == subagent_id)
        return statement

    async def list_for_board(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        limit: int,
        completed: bool | None,
        offset: int = 0,
        subagent_id: str | None = None,
    ) -> list[RowMapping]:
        latest_post_id = (
            select(SubagentBoardPost.id)
            .where(
                SubagentBoardPost.uid == uid,
                SubagentBoardPost.session_id == session_id,
                SubagentBoardPost.profile_id == profile_id,
                SubagentBoardPost.subagent_id == Subagent.id,
                SubagentBoardPost.child_work_id == SubagentWork.child_work_id,
            )
            .order_by(SubagentBoardPost.id.desc())
            .limit(1)
            .correlate(Subagent, SubagentWork)
            .scalar_subquery()
        )
        updated_at = case(
            (SubagentBoardPost.created_at > SessionReplyWorkItem.updated_at, SubagentBoardPost.created_at),
            else_=SessionReplyWorkItem.updated_at,
        ).label("updated_at")
        round_completed = _board_round_completed()
        statement = select(
            Subagent.id.label("subagent_id"),
            Subagent.name.label("name"),
            SubagentWork.child_work_id.label("work_id"),
            SubagentWork.created_at.label("created_at"),
            updated_at,
            round_completed.label("completed"),
            SubagentWork.result["status"].as_string().label("result_status"),
            SessionReplyWorkItem.status.label("work_status"),
            SessionReplyWorkItem.lock_until.label("lock_until"),
            SessionReplyWorkItem.result_message_id.label("result_message_id"),
            SessionReplyWorkItem.execution_state["response"]["skip_reply"].as_boolean().label("skip_reply"),
            SubagentWork.waiting_confirmation.label("waiting_confirmation"),
            SubagentBoardPost.id.label("latest_post_id"),
            SubagentBoardPost.stage.label("post_stage"),
            func.substr(SubagentBoardPost.content, 1, SUBAGENT_BOARD_PREVIEW_CHARS).label("post_preview"),
        )
        statement = self._board_scope(
            statement,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            subagent_id=subagent_id,
        ).outerjoin(SubagentBoardPost, SubagentBoardPost.id == latest_post_id)

        if completed is True:
            statement = statement.where(round_completed).order_by(updated_at.desc(), SubagentWork.child_work_id.desc())
        elif completed is False:
            statement = statement.where(~round_completed).order_by(
                case(
                    (SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM, 1),
                    else_=0,
                ).asc(),
                SubagentWork.child_work_id.asc(),
            )

        result = await db.execute(statement.offset(offset).limit(limit))
        return list(result.mappings().all())

    async def count_active_for_board(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> int:
        statement = self._board_scope(
            select(func.count()),
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        ).where(~_board_round_completed())
        result = await db.execute(statement)
        return int(result.scalar_one())

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

    async def list_board_rounds(
        self,
        db: AsyncSession,
        *,
        subagent_id: str,
        limit: int,
        before_id: int | None = None,
    ) -> list[tuple[int, datetime]]:
        statement = select(SubagentWork.child_work_id, SubagentWork.created_at).where(SubagentWork.subagent_id == subagent_id)
        if before_id is not None:
            statement = statement.where(SubagentWork.child_work_id < before_id)
        result = await db.execute(statement.order_by(SubagentWork.child_work_id.desc()).limit(limit))
        return list(result.all())

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

    async def list_round_work_items(
        self,
        db: AsyncSession,
        *,
        subagent_id: str,
        child_work_id: int,
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> list[SessionReplyWorkItem]:
        result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.subagent_id == subagent_id,
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
                _subagent_round_work_condition(SessionReplyWorkItem, child_work_id),
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

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
        if result.rowcount == 1:
            notify_after_commit(
                db,
                NotificationTopic.SESSION_REPLY,
                event="capacity",
                resource_type="session_reply_work",
                resource_id=work.id,
                uid=work.uid,
                session_id=work.session_id,
                profile_id=work.profile_id,
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

    async def list_parent_ids_for_child(self, db: AsyncSession, *, child_work_id: int, undelivered_only: bool = True) -> list[int]:
        conditions = [SubagentToolCall.child_work_id == child_work_id]
        if undelivered_only:
            conditions.append(SubagentToolCall.delivered.is_(False))
        result = await db.execute(select(SubagentToolCall.parent_work_id).where(*conditions).distinct().order_by(SubagentToolCall.parent_work_id.asc()))
        return list(result.scalars().all())

    async def list_pending_parent_ids(self, db: AsyncSession, *, after_id: int = 0, limit: int = 100) -> list[int]:
        result = await db.execute(
            select(SubagentToolCall.parent_work_id)
            .join(SubagentWork, SubagentWork.child_work_id == SubagentToolCall.child_work_id)
            .join(SessionReplyWorkItem, SessionReplyWorkItem.id == SubagentToolCall.parent_work_id)
            .where(
                SubagentToolCall.delivered.is_(False),
                SessionReplyWorkItem.status.in_(
                    (
                        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                        SessionReplyWorkStatus.READY_FOR_LLM,
                        SessionReplyWorkStatus.SUCCEEDED,
                    )
                ),
                or_(
                    SubagentWork.result.is_not(None),
                    exists(
                        select(1)
                        .select_from(SubagentBoardPost)
                        .where(
                            SubagentBoardPost.child_work_id == SubagentWork.child_work_id,
                            SubagentBoardPost.subagent_id == SubagentWork.subagent_id,
                        )
                    ),
                ),
                SubagentToolCall.parent_work_id > after_id,
            )
            .distinct()
            .order_by(SubagentToolCall.parent_work_id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def create_board_post(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        subagent_id: str,
        child_work_id: int,
        stage: str,
        content: str,
    ) -> SubagentBoardPost:
        subagent = await self.get_scoped(
            db,
            subagent_id=subagent_id,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            lock=False,
            touch=False,
        )
        if subagent is None:
            raise ValueError(f"Scoped subagent not found for subagent_id={subagent_id}")
        round_row = await self.get_round(db, child_work_id=child_work_id, subagent_id=subagent_id)
        if round_row is None:
            raise ValueError(f"SubagentWork round not found for subagent_id={subagent_id}, child_work_id={child_work_id}")
        work_item = await session_reply_work_item_crud.get(db, child_work_id)
        if work_item is None or work_item.uid != uid or work_item.session_id != session_id or work_item.profile_id != profile_id:
            raise ValueError(f"Work item scope mismatch for child_work_id={child_work_id}")
        post = SubagentBoardPost(
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            subagent_id=subagent_id,
            child_work_id=child_work_id,
            stage=stage,
            content=content,
        )
        db.add(post)
        await db.flush()
        await bump_subagent_board_revisions(db, [session_id])
        return post

    async def list_board_posts(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        subagent_id: str,
        child_work_id: int | None = None,
    ) -> list[SubagentBoardPost]:
        conditions = [
            SubagentBoardPost.uid == uid,
            SubagentBoardPost.session_id == session_id,
            SubagentBoardPost.profile_id == profile_id,
            SubagentBoardPost.subagent_id == subagent_id,
        ]
        if child_work_id is not None:
            conditions.append(SubagentBoardPost.child_work_id == child_work_id)
        result = await db.execute(select(SubagentBoardPost).where(*conditions).order_by(SubagentBoardPost.id.asc()).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def list_board_posts_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        subagent_id: str,
        child_work_id: int,
        before_id: int | None = None,
        limit: int,
    ) -> list[RowMapping]:
        conditions = [
            SubagentBoardPost.uid == uid,
            SubagentBoardPost.session_id == session_id,
            SubagentBoardPost.profile_id == profile_id,
            SubagentBoardPost.subagent_id == subagent_id,
            SubagentBoardPost.child_work_id == child_work_id,
        ]
        if before_id is not None:
            conditions.append(SubagentBoardPost.id < before_id)
        result = await db.execute(
            select(
                SubagentBoardPost.id,
                SubagentBoardPost.child_work_id,
                SubagentBoardPost.stage,
                func.substr(SubagentBoardPost.content, 1, SUBAGENT_BOARD_POST_DISPLAY_CHARS + 1).label("content"),
                SubagentBoardPost.created_at,
            )
            .where(*conditions)
            .order_by(SubagentBoardPost.id.desc())
            .limit(limit)
        )
        return list(result.mappings().all())

    async def get_latest_board_post(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        subagent_id: str,
        child_work_id: int | None = None,
    ) -> SubagentBoardPost | None:
        conditions = [
            SubagentBoardPost.uid == uid,
            SubagentBoardPost.session_id == session_id,
            SubagentBoardPost.profile_id == profile_id,
            SubagentBoardPost.subagent_id == subagent_id,
        ]
        if child_work_id is not None:
            conditions.append(SubagentBoardPost.child_work_id == child_work_id)
        result = await db.execute(select(SubagentBoardPost).where(*conditions).order_by(SubagentBoardPost.id.desc()).limit(1).execution_options(populate_existing=True))
        return result.scalars().first()


subagent_crud = CRUDSubagent()
