from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKeyConstraint, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.context_summary_fragment import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
)
from app.core.crud.context_summary_stage import context_summary_stage_crud
from app.core.crud.scheduled_task import scheduled_task_crud
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.core.utils.context_summary.stage import build_summary_work_identity
from app.models import (
    ChatSession,
    ContextSummaryFragment,
    ContextSummaryStage,
    Profile,
    PromptLibrary,
    ScheduledTask,
    SessionEvent,
    SessionReplyProviderUsage,
    SessionReplyWorkItem,
)
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
        "message",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("session_reply_stream_event", ("work_id",), "session_reply_work_item", ("id",), "CASCADE"),
    ("scheduled_task", ("profile_id",), "profile", ("id",), "RESTRICT"),
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
    ("session_reply_provider_usage", ("work_id", "session_id", "uid"), "session_reply_work_item", ("id", "session_id", "uid"), "CASCADE"),
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
async def db_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    PromptLibrary.__table__,
                    Profile.__table__,
                    ChatSession.__table__,
                    SessionReplyWorkItem.__table__,
                    SessionReplyProviderUsage.__table__,
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                    SessionEvent.__table__,
                    ScheduledTask.__table__,
                ],
            )
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


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
        profile_id=profile.id,
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
async def test_provider_usage_cascades_with_reply_work_but_session_totals_remain(db_session_factory):
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

        provider_usage = SessionReplyProviderUsage(
            provider_request_id="request-usage-1",
            work_id=77,
            session_id="session-usage",
            uid="user-1",
            input_tokens=100,
            cached_tokens=25,
            output_tokens=7,
        )
        db.add(provider_usage)
        await db.commit()
        provider_usage_id = provider_usage.id
        assert provider_usage_id is not None

        await db.delete(work_item)
        await db.commit()

    async with db_session_factory() as check_db:
        assert await check_db.get(SessionReplyProviderUsage, provider_usage_id) is None
        chat_session = await check_db.get(ChatSession, "session-usage")
        assert chat_session is not None
        assert chat_session.llm_request_metadata["total_input_tokens"] == 100
        assert chat_session.llm_request_metadata["total_cached_tokens"] == 25
        assert chat_session.llm_request_metadata["total_output_tokens"] == 7


@pytest.mark.asyncio
async def test_sqlite_restricts_profile_deletion_with_scheduled_task(db_session_factory):
    async with db_session_factory() as db:
        profile, _chat_session, scheduled_task = await _create_profile_with_scheduled_task(db)
        profile_id = profile.id
        scheduled_task_id = scheduled_task.id
        assert profile_id is not None
        assert scheduled_task_id is not None

        await db.delete(profile)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        assert await db.get(Profile, profile_id) is not None
        assert await db.get(ScheduledTask, scheduled_task_id) is not None


@pytest.mark.asyncio
async def test_scheduled_task_crud_has_profile_assignment(db_session_factory):
    async with db_session_factory() as db:
        profile, _chat_session, _scheduled_task = await _create_profile_with_scheduled_task(db)
        assert profile.id is not None

        assert await scheduled_task_crud.has_profile_assignment(db, profile.id) is True
        assert await scheduled_task_crud.has_profile_assignment(db, profile.id + 1) is False


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
