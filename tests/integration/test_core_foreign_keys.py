from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import SQLModel, select

from app.core.crud.context_summary.fragment import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
)
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.crud.provider_usage import provider_usage_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.core.utils.context_summary.stage import build_summary_work_identity
from app.models import (
    ChatSession,
    ContextSummaryFragment,
    ContextSummaryStage,
    Profile,
    PromptLibrary,
    ProviderUsageDaily,
    ProviderUsageDetail,
    ScheduledTask,
    SessionEvent,
    SessionReplyWorkItem,
)
from app.models.provider_usage import ProviderUsageType
from app.models.scheduled_task import ScheduledTaskStatus
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkType

EXPECTED_FOREIGN_KEY_CONSTRAINTS = {
    # audit
    ("audit_tool_detail", ("audit_record_id",), "audit_record", ("id",), "CASCADE"),
    (
        "audit_confirmation_claim",
        ("audit_record_id", "uid", "session_id"),
        "audit_record",
        ("id", "uid", "session_id"),
        "CASCADE",
    ),
    (
        "audit_execution_record",
        ("audit_tool_detail_id", "audit_record_id"),
        "audit_tool_detail",
        ("id", "audit_record_id"),
        "CASCADE",
    ),
    ("audit_execution_record", ("audit_record_id",), "audit_record", ("id",), "CASCADE"),
    # knowledge base
    ("knowledge_base", ("embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    (
        "knowledge_base",
        ("managed_profile_id", "uid"),
        "profile",
        ("id", "uid"),
        "CASCADE",
    ),
    ("knowledge_base", ("active_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("knowledge_base", ("target_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    (
        "knowledge_base_profile_binding",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    (
        "knowledge_base_profile_binding",
        ("profile_id", "uid"),
        "profile",
        ("id", "uid"),
        "CASCADE",
    ),
    ("knowledge_base_collection_owner", ("knowledge_base_id",), "knowledge_base", ("id",), "SET NULL"),
    ("knowledge_base_document", ("knowledge_base_id",), "knowledge_base", ("id",), "CASCADE"),
    (
        "managed_knowledge_item",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    (
        "managed_knowledge_revision",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    (
        "knowledge_job",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    (
        "knowledge_base_embedding_delta",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    (
        "knowledge_organization_snapshot",
        ("knowledge_base_id", "uid"),
        "knowledge_base",
        ("id", "uid"),
        "CASCADE",
    ),
    ("knowledge_organization_snapshot_item", ("snapshot_id",), "knowledge_organization_snapshot", ("id",), "CASCADE"),
    ("knowledge_organization_stage", ("snapshot_id",), "knowledge_organization_snapshot", ("id",), "CASCADE"),
    ("knowledge_organization_fragment", ("stage_id",), "knowledge_organization_stage", ("id",), "CASCADE"),
    # session, profile, platform, and scheduled task
    ("profile", ("prompt_id",), "prompt", ("id",), "RESTRICT"),
    ("chat_session", ("profile_override_id",), "profile", ("id",), "RESTRICT"),
    ("message_platform", ("profile_id",), "profile", ("id",), "RESTRICT"),
    (
        "session_event",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    (
        "session_todo_plan",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    (
        "message",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("session_reply_stream_event", ("work_id",), "session_reply_work_item", ("id",), "CASCADE"),
    (
        "scheduled_task",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    (
        "message_platform_outbox",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    # context summary and reply work
    (
        "session_reply_work_item",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("session_reply_sequence", ("session_id",), "chat_session", ("session_id",), "CASCADE"),
    # terminal
    (
        "terminal_session",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("terminal_control_command", ("terminal_session_id",), "terminal_session", ("terminal_session_id",), "CASCADE"),
    # long-term memory
    ("long_term_memory_store", ("active_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_store", ("target_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_store", ("organization_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_embedding_selection_token", ("profile_id",), "profile", ("id",), "CASCADE"),
    (
        "long_term_memory_embedding_selection_token",
        ("target_embedding_channel_id",),
        "channel",
        ("id",),
        "CASCADE",
    ),
}


def _normalize_foreign_key_constraints() -> set[tuple[str, tuple[str, ...], str, tuple[str, ...], str | None]]:
    normalized = set()
    for table in SQLModel.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            elements = tuple(constraint.elements)
            normalized.add(
                (
                    table.name,
                    tuple(element.parent.name for element in elements),
                    elements[0].column.table.name,
                    tuple(element.column.name for element in elements),
                    elements[0].ondelete,
                )
            )
    return normalized


def test_core_foreign_key_metadata_matches_design():
    assert _normalize_foreign_key_constraints() == EXPECTED_FOREIGN_KEY_CONSTRAINTS


@pytest_asyncio.fixture
async def db_session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            SessionReplyWorkItem.__table__,
            ProviderUsageDetail.__table__,
            ProviderUsageDaily.__table__,
            ContextSummaryStage.__table__,
            ContextSummaryFragment.__table__,
            SessionEvent.__table__,
            ScheduledTask.__table__,
        ],
        foreign_keys=True,
    ) as factory:
        yield factory


async def _create_profile_with_scheduled_task(db: AsyncSession) -> tuple[Profile, ChatSession, ScheduledTask]:
    profile = Profile(uid="user-1", name="test-profile", configs={})
    chat_session = ChatSession(session_id="session-1", uid="user-1")
    db.add_all([profile, chat_session])
    await db.flush()
    assert profile.id is not None

    scheduled_task = ScheduledTask(
        name="test-task",
        uid="user-1",
        session_id="session-1",
        message="run",
        interval_seconds=60,
        status=ScheduledTaskStatus.ENABLED,
    )
    db.add(scheduled_task)
    await db.commit()
    return profile, chat_session, scheduled_task


@pytest.mark.asyncio
async def test_sqlite_enforces_session_owner_foreign_key_and_cascade(db_session_factory):
    async with db_session_factory() as db:
        chat_session = ChatSession(session_id="session-1", uid="user-1")
        session_event = SessionEvent(
            dedupe_key="event-1",
            uid="user-1",
            session_id="session-1",
            event={"type": "test"},
        )
        db.add(chat_session)
        await db.flush()
        db.add(session_event)
        await db.commit()
        session_event_id = session_event.id
        assert session_event_id is not None

        db.add(
            SessionEvent(
                dedupe_key="event-2",
                uid="wrong-user",
                session_id="session-1",
                event={"type": "test"},
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        await db.delete(chat_session)
        await db.commit()
        assert await db.get(SessionEvent, session_event_id) is None


@pytest.mark.asyncio
async def test_provider_usage_survives_reply_work_deletion_and_session_totals_remain(db_session_factory):
    async with db_session_factory() as db:
        chat_session = ChatSession(
            session_id="session-usage",
            uid="user-1",
            llm_request_metadata={
                "input_tokens": 100,
                "input_tokens_source": "provider",
                "total_input_tokens": 100,
                "total_cached_tokens": 25,
                "cache_hit_rate": 0.25,
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "total_output_tokens": 7,
            },
        )
        db.add(chat_session)
        await db.flush()

        work_item = SessionReplyWorkItem(
            id=77,
            uid="user-1",
            session_id="session-usage",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="1",
            dedupe_key="provider-usage-work-77",
        )
        db.add(work_item)
        await db.flush()

        provider_usage = ProviderUsageDetail(
            provider_request_id="request-usage-1",
            created_at=datetime(2026, 1, 1, 12, 0, 0),
            type=ProviderUsageType.MAIN_DIALOGUE.value,
            uid="user-1",
            session_id="session-usage",
            work_id=77,
            input_tokens=100,
            cached_tokens=25,
            output_tokens=7,
        )
        assert await provider_usage_crud.create_once(db, provider_usage) is True
        await db.commit()

        await db.delete(work_item)
        await db.commit()

    async with db_session_factory() as check_db:
        assert await check_db.get(SessionReplyWorkItem, 77) is None
        persisted_usage = await check_db.get(ProviderUsageDetail, "request-usage-1")
        assert persisted_usage is not None
        assert (
            persisted_usage.work_id,
            persisted_usage.type,
            persisted_usage.input_tokens,
            persisted_usage.cached_tokens,
            persisted_usage.output_tokens,
        ) == (77, ProviderUsageType.MAIN_DIALOGUE.value, 100, 25, 7)
        daily_usage = (await check_db.execute(select(ProviderUsageDaily))).scalars().one()
        assert (
            daily_usage.type,
            daily_usage.input_tokens,
            daily_usage.cached_tokens,
            daily_usage.output_tokens,
            daily_usage.request_count,
        ) == (ProviderUsageType.MAIN_DIALOGUE.value, 100, 25, 7, 1)
        chat_session = await check_db.get(ChatSession, "session-usage")
        assert chat_session is not None
        assert chat_session.llm_request_metadata["total_input_tokens"] == 100
        assert chat_session.llm_request_metadata["total_cached_tokens"] == 25
        assert chat_session.llm_request_metadata["total_output_tokens"] == 7


@pytest.mark.asyncio
async def test_sqlite_profile_deletion_allows_scheduled_task_and_session_deletion_cascades(db_session_factory):
    async with db_session_factory() as db:
        profile, _chat_session, scheduled_task = await _create_profile_with_scheduled_task(db)
        profile_id = profile.id
        scheduled_task_id = scheduled_task.id
        assert profile_id is not None
        assert scheduled_task_id is not None

        await db.delete(profile)
        await db.commit()

    async with db_session_factory() as check_db:
        assert await check_db.get(Profile, profile_id) is None
        assert await check_db.get(ScheduledTask, scheduled_task_id) is not None

        chat_session = await check_db.get(ChatSession, "session-1")
        assert chat_session is not None
        await check_db.delete(chat_session)
        await check_db.commit()

    async with db_session_factory() as check_db:
        assert await check_db.get(ScheduledTask, scheduled_task_id) is None


@pytest.mark.asyncio
async def test_scheduled_task_crud_list_by_sessions_filters_uid_and_orders_ids(db_session_factory):
    async with db_session_factory() as db:
        db.add_all(
            [
                ChatSession(session_id="session-1", uid="user-1"),
                ChatSession(session_id="session-2", uid="user-1"),
                ChatSession(session_id="session-3", uid="user-1"),
                ChatSession(session_id="session-other-user", uid="user-2"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                ScheduledTask(
                    id=20,
                    name="selected-later",
                    uid="user-1",
                    session_id="session-1",
                    message="run",
                    interval_seconds=60,
                ),
                ScheduledTask(
                    id=10,
                    name="selected-earlier",
                    uid="user-1",
                    session_id="session-2",
                    message="run",
                    interval_seconds=60,
                ),
                ScheduledTask(
                    id=5,
                    name="unselected",
                    uid="user-1",
                    session_id="session-3",
                    message="run",
                    interval_seconds=60,
                ),
                ScheduledTask(
                    id=30,
                    name="other-user",
                    uid="user-2",
                    session_id="session-other-user",
                    message="run",
                    interval_seconds=60,
                ),
            ]
        )
        await db.commit()

        assert await scheduled_task_crud.list_by_sessions(db, uid="user-1", session_ids=[]) == []
        tasks = await scheduled_task_crud.list_by_sessions(
            db,
            uid="user-1",
            session_ids=["session-other-user", "session-2", "session-1"],
        )

        assert [task.id for task in tasks] == [10, 20]


@pytest.mark.asyncio
async def test_context_summary_stage_and_fragment_do_not_require_reply_work_item(db_session_factory):
    session_id = "session-1"
    uid = "user-1"
    snapshot = ContextSummarySnapshot(
        expected_summary_message_id=None,
        snapshot_before_id=None,
        snapshot_max_message_id=1,
        persistent_summary_target_id=1,
        recent_round_start_ids=(),
        frozen_user_message_ids=(),
        recent_messages=(),
    )
    work_id, work_dedupe_key, snapshot_key = build_summary_work_identity(
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        revision=0,
    )
    stage_key = "stage-0"
    model_key = "model-key"

    async with db_session_factory() as db:
        db.add(ChatSession(session_id=session_id, uid=uid))
        await db.flush()
        assert await db.get(SessionReplyWorkItem, work_id) is None

        stage = ContextSummaryStage(
            uid=uid,
            session_id=session_id,
            work_id=work_id,
            work_dedupe_key=work_dedupe_key,
            snapshot_key=snapshot_key,
            stage_key=stage_key,
            model_key=model_key,
            channel_id=1,
            model_id="summary-model",
            context_window_k=128,
            max_output_tokens=4096,
            safety_margin_tokens=512,
            expected_summary_message_id=snapshot.expected_summary_message_id,
            expected_summary_revision=0,
            expected_content_revision=snapshot.content_revision,
            snapshot_max_message_id=snapshot.snapshot_max_message_id,
            persistent_summary_target_id=snapshot.persistent_summary_target_id,
            expected_fragment_count=1,
        )
        persisted_stage, stage_created = await context_summary_stage_crud.create_stage(
            db,
            stage=stage,
        )
        assert stage_created is True
        assert persisted_stage.id is not None

        fragment = ContextSummaryFragment(
            dedupe_key=build_context_summary_fragment_dedupe_key(
                work_dedupe_key=work_dedupe_key,
                stage_key=stage_key,
                model_key=model_key,
                fragment_index=0,
            ),
            uid=uid,
            session_id=session_id,
            work_id=work_id,
            work_dedupe_key=work_dedupe_key,
            snapshot_key=snapshot_key,
            stage_key=stage_key,
            model_key=model_key,
            fragment_index=0,
            message_start_id=1,
            message_end_id=1,
            channel_id=1,
            model_id="summary-model",
            token_count=1,
            content="summary",
        )
        persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
            db,
            fragment=fragment,
        )
        assert fragment_created is True
        assert persisted_fragment is not None
        assert persisted_fragment.id is not None
