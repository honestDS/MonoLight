from typing import Any

from sqlalchemy import exists, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import ensure_sqlite_outer_transaction


def context_summary_running_expression():
    latest_summary_type = (
        select(SessionReplyStreamEvent.event["type"].as_string())
        .where(
            SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id,
            SessionReplyStreamEvent.event["type"].as_string().in_(["context_summary_start", "context_summary_end"]),
            SessionReplyStreamEvent.event["work_sequence_no"].as_integer() == SessionReplyWorkItem.sequence_no,
        )
        .order_by(SessionReplyStreamEvent.sequence_no.desc())
        .limit(1)
        .correlate(SessionReplyWorkItem)
        .scalar_subquery()
    )
    return exists(
        select(SessionReplyWorkItem.id)
        .where(
            SessionReplyWorkItem.session_id == ChatSession.session_id,
            SessionReplyWorkItem.uid == ChatSession.uid,
            SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
            SessionReplyWorkItem.work_type.in_(
                [
                    SessionReplyWorkType.FOREGROUND_REPLY,
                    SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                ]
            ),
            latest_summary_type == "context_summary_start",
        )
        .correlate(ChatSession)
    ).label("is_context_summarizing")


class CRUDSessionReplyStreamEvent:
    async def publish(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        sequence_no: int,
        event: dict[str, Any],
        commit: bool = True,
    ) -> tuple[SessionReplyStreamEvent, bool]:
        item = SessionReplyStreamEvent(work_id=work_id, sequence_no=sequence_no, event=event)
        await ensure_sqlite_outer_transaction(db)
        try:
            async with db.begin_nested():
                db.add(item)
                await db.flush()
        except IntegrityError:
            existing = await db.execute(
                select(SessionReplyStreamEvent)
                .where(
                    SessionReplyStreamEvent.work_id == work_id,
                    SessionReplyStreamEvent.sequence_no == sequence_no,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            item = existing.scalars().first()
            if item is None:
                raise
            created = False
        else:
            created = True
        if commit:
            await db.commit()
        return item, created

    async def get_latest_sequence(self, db: AsyncSession, *, work_id: int) -> int:
        result = await db.execute(select(func.max(SessionReplyStreamEvent.sequence_no)).where(SessionReplyStreamEvent.work_id == work_id))
        return int(result.scalar() or 0)

    async def get_latest_resume_boundary_sequence(self, db: AsyncSession, *, work_id: int, history_message_id: int) -> int:
        result = await db.execute(
            select(func.max(SessionReplyStreamEvent.sequence_no)).where(
                SessionReplyStreamEvent.work_id == work_id,
                SessionReplyStreamEvent.event["type"].as_string() == "turn_end",
                SessionReplyStreamEvent.event["message_id"].as_integer() <= history_message_id,
            )
        )
        return int(result.scalar() or 0)

    async def has_unseen_persisted_turn(self, db: AsyncSession, *, work_id: int, history_message_id: int) -> bool:
        result = await db.execute(
            select(SessionReplyStreamEvent.id)
            .where(
                SessionReplyStreamEvent.work_id == work_id,
                SessionReplyStreamEvent.event["type"].as_string() == "turn_end",
                SessionReplyStreamEvent.event["message_id"].as_integer() > history_message_id,
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def has_events(self, db: AsyncSession, *, work_id: int) -> bool:
        result = await db.execute(select(SessionReplyStreamEvent.id).where(SessionReplyStreamEvent.work_id == work_id).limit(1))
        return result.scalar() is not None

    async def list_after_sequence(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        after_sequence_no: int,
        limit: int = 100,
    ) -> list[SessionReplyStreamEvent]:
        result = await db.execute(
            select(SessionReplyStreamEvent)
            .where(
                SessionReplyStreamEvent.work_id == work_id,
                SessionReplyStreamEvent.sequence_no > after_sequence_no,
            )
            .order_by(SessionReplyStreamEvent.sequence_no)
            .limit(limit)
        )
        return list(result.scalars().all())


session_reply_stream_event_crud = CRUDSessionReplyStreamEvent()
