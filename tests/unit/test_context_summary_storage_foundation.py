import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.models.audit import AuditToolResultVersion
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.user import User


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    Message.__table__,
                    ChatSession.__table__,
                    SessionReplyWorkItem.__table__,
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                    AuditToolResultVersion.__table__,
                    User.__table__,
                    Profile.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                Profile(id=1, uid="user-1", name="profile-1", configs={}),
                Profile(id=2, uid="user-1", name="profile-2", configs={}),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_context_summary_update_compares_boundary_and_revision_atomically(
    db_session: AsyncSession,
):
    session = ChatSession(
        session_id="session-1",
        uid="user-1",
        context_summary="old summary",
        context_summary_message_id=8,
        context_summary_revision=3,
    )
    db_session.add(session)
    await db_session.commit()

    first_updated = await session_crud.update_context_summary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_message_id=8,
        expected_revision=3,
        expected_content_revision=0,
        summary="first candidate",
        message_id=12,
    )
    await db_session.commit()

    stale_same_boundary_updated = await session_crud.update_context_summary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_message_id=12,
        expected_revision=3,
        expected_content_revision=0,
        summary="stale candidate",
        message_id=12,
    )
    await db_session.rollback()

    await db_session.refresh(session)
    assert first_updated is True
    assert stale_same_boundary_updated is False
    assert session.context_summary == "first candidate"
    assert session.context_summary_message_id == 12
    assert session.context_summary_revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_show_tool_calls"),
    [
        ("http", True),
        ("ws", True),
        ("weixin-openclaw", False),
        ("future-platform", False),
    ],
)
async def test_upsert_profile_sets_tool_call_visibility_by_source(
    db_session: AsyncSession,
    source: str,
    expected_show_tool_calls: bool,
):
    session = await session_crud.upsert_profile(
        db_session,
        session_id=f"session-{source}",
        uid="user-1",
        profile_id=1,
        source=source,
    )

    assert session.show_tool_calls is expected_show_tool_calls


@pytest.mark.asyncio
async def test_upsert_profile_preserves_existing_external_tool_call_visibility(db_session: AsyncSession):
    session = await session_crud.upsert_profile(
        db_session,
        session_id="session-future-platform",
        uid="user-1",
        profile_id=1,
        source="future-platform",
    )
    session.show_tool_calls = True
    await db_session.flush()

    updated_session = await session_crud.upsert_profile(
        db_session,
        session_id="session-future-platform",
        uid="user-1",
        profile_id=2,
        source="future-platform",
    )

    assert updated_session.show_tool_calls is True
    assert updated_session.profile_id == 2


@pytest.mark.asyncio
async def test_tool_result_version_invalidates_summary_and_preserves_previous_content(db_session: AsyncSession):
    message = Message(
        id=10,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content='{"role":"tool","tool_call_id":"call-1","content":"{\\"status\\":\\"pending\\"}"}',
        audit_record_id=20,
        audit_tool_call_id="call-1",
        content_revision=0,
        is_processed=True,
    )
    session = ChatSession(
        session_id="session-1",
        uid="user-1",
        context_summary="old summary",
        context_summary_message_id=8,
        context_summary_revision=3,
        context_content_revision=1,
    )
    db_session.add_all(
        [
            message,
            session,
            AuditToolResultVersion(
                uid="user-1",
                session_id="session-1",
                audit_record_id=20,
                source_assistant_message_id=9,
                original_tool_call_id="call-1",
                message_id=10,
                version_no=0,
                content=message.content,
            ),
        ]
    )
    await db_session.commit()

    from app.core.crud.audit_tool_result_version import audit_tool_result_version_crud

    await audit_tool_result_version_crud.append_version(
        db_session,
        uid="user-1",
        session_id="session-1",
        audit_record_id=20,
        source_assistant_message_id=9,
        original_tool_call_id="call-1",
        message_id=10,
        content='{"role":"tool","tool_call_id":"call-1","content":"{\\"status\\":\\"succeeded\\"}"}',
    )

    await db_session.refresh(message)
    await db_session.refresh(session)
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == 20).order_by(AuditToolResultVersion.version_no))).scalars().all())
    assert len(versions) == 2
    assert json.loads(versions[0].content)["content"] == '{"status":"pending"}'
    assert json.loads(message.content)["content"] == '{"status":"succeeded"}'
    assert session.context_summary is None
    assert session.context_summary_message_id is None
    assert session.context_content_revision == 2
    assert session.context_summary_revision == 4


@pytest.mark.asyncio
async def test_llm_request_metadata_update_persists_supported_baseline_fields(db_session: AsyncSession):
    session = ChatSession(session_id="session-1", uid="user-1")
    db_session.add(session)
    await db_session.commit()

    updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "type": "llm_request_metadata",
            "turn": 2,
            "response_id": "response-1",
            "input_tokens": 123,
            "input_tokens_source": "provider",
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "output_tokens": 77,
            "total_output_tokens": 567,
            "total_input_tokens": 1000,
            "total_cached_tokens": 325,
            "cached_tokens": 40,
            "cache_hit_rate": 0.325,
            "request_message_min_id": 10,
            "request_message_max_id": 20,
            "model_id": "grok-4.5",
            "protocol": "openai",
            "context_summary_revision": 2,
            "context_content_revision": 3,
            "system_tokens": 50,
            "tools_tokens": 60,
        },
    )
    await db_session.refresh(session)

    assert updated is True
    assert session.llm_request_metadata == {
        "input_tokens": 123,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        "request_message_min_id": 10,
        "request_message_max_id": 20,
        "context_summary_revision": 2,
        "context_content_revision": 3,
        "system_tokens": 50,
        "tools_tokens": 60,
        "output_tokens": 77,
        "total_output_tokens": 567,
        "total_input_tokens": 1000,
        "total_cached_tokens": 325,
        "cached_tokens": 40,
        "cache_hit_rate": 0.325,
        "model_id": "grok-4.5",
        "protocol": "openai",
        "input_tokens_source": "provider",
    }

    invalid_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": True,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
        },
    )
    invalid_cache_hit_rate_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "cache_hit_rate": 1.01,
        },
    )
    invalid_total_output_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "total_output_tokens": -1,
        },
    )
    invalid_total_output_bool_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "total_output_tokens": True,
        },
    )
    invalid_total_input_tokens_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "total_input_tokens": 1000,
        },
    )
    invalid_total_cached_tokens_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "total_cached_tokens": 325,
        },
    )
    invalid_total_cached_tokens_exceed_input_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata={
            "input_tokens": 123,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "total_input_tokens": 1000,
            "total_cached_tokens": 1001,
        },
    )
    await db_session.refresh(session)

    assert invalid_updated is False
    assert invalid_cache_hit_rate_updated is False
    assert invalid_total_output_updated is False
    assert invalid_total_output_bool_updated is False
    assert invalid_total_input_tokens_updated is False
    assert invalid_total_cached_tokens_updated is False
    assert invalid_total_cached_tokens_exceed_input_updated is False
    assert session.llm_request_metadata == {
        "input_tokens": 123,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        "request_message_min_id": 10,
        "request_message_max_id": 20,
        "context_summary_revision": 2,
        "context_content_revision": 3,
        "system_tokens": 50,
        "tools_tokens": 60,
        "output_tokens": 77,
        "total_output_tokens": 567,
        "total_input_tokens": 1000,
        "total_cached_tokens": 325,
        "cached_tokens": 40,
        "cache_hit_rate": 0.325,
        "model_id": "grok-4.5",
        "protocol": "openai",
        "input_tokens_source": "provider",
    }


@pytest.mark.asyncio
async def test_llm_request_metadata_update_rejects_stale_work_and_event_sequences(db_session: AsyncSession):
    session = ChatSession(session_id="session-1", uid="user-1")
    db_session.add(session)
    await db_session.commit()

    def metadata(*, work_sequence_no: int, event_sequence_no: int, input_tokens: int) -> dict:
        return {
            "type": "llm_request_metadata",
            "input_tokens": input_tokens,
            "context_window_tokens": 4096,
            "max_output_tokens": 512,
            "work_id": 7,
            "work_sequence_no": work_sequence_no,
            "event_sequence_no": event_sequence_no,
        }

    first_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata=metadata(work_sequence_no=3, event_sequence_no=5, input_tokens=100),
    )
    stale_work_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata=metadata(work_sequence_no=2, event_sequence_no=99, input_tokens=200),
    )
    stale_event_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata=metadata(work_sequence_no=3, event_sequence_no=4, input_tokens=300),
    )
    newer_event_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata=metadata(work_sequence_no=3, event_sequence_no=6, input_tokens=400),
    )
    newer_work_updated = await session_crud.update_llm_request_metadata(
        db_session,
        session_id="session-1",
        uid="user-1",
        metadata=metadata(work_sequence_no=4, event_sequence_no=1, input_tokens=500),
    )
    await db_session.refresh(session)

    assert first_updated is True
    assert stale_work_updated is False
    assert stale_event_updated is False
    assert newer_event_updated is True
    assert newer_work_updated is True
    assert session.llm_request_metadata_work_sequence_no == 4
    assert session.llm_request_metadata_event_sequence_no == 1
    assert session.llm_request_metadata == {
        "input_tokens": 500,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        "work_sequence_no": 4,
        "event_sequence_no": 1,
    }


@pytest.mark.asyncio
async def test_user_sessions_include_llm_request_metadata(db_session: AsyncSession):
    db_session.add(User(uid="user-1", username="alice"))
    db_session.add(
        ChatSession(
            session_id="session-1",
            uid="user-1",
            title="Session 1",
            llm_request_metadata={
                "input_tokens": 321,
                "context_window_tokens": 8192,
                "max_output_tokens": 1024,
            },
        )
    )
    db_session.add_all(
        [
            Message(
                session_id="session-1",
                uid="user-1",
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="hello",
                profile_id=1,
            ),
            Message(
                session_id="session-1",
                uid="user-1",
                role=MessageRole.ASSISTANT,
                type=MessageType.TEXT,
                content="hi",
                profile_id=1,
            ),
        ]
    )
    await db_session.commit()

    sessions = await message_crud.get_user_sessions(db_session, uid="user-1")

    assert len(sessions) == 1
    assert sessions[0].session_id == "session-1"
    assert sessions[0].username == "alice"
    assert sessions[0].llm_request_metadata == {
        "input_tokens": 321,
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
    }


@pytest.mark.asyncio
async def test_user_sessions_include_owned_empty_sessions(db_session: AsyncSession):
    db_session.add_all(
        [
            User(uid="user-1", username="alice"),
            User(uid="user-2", username="bob"),
            ChatSession(
                session_id="empty-session",
                uid="user-1",
                profile_id=3,
                profile_override_id=4,
            ),
            ChatSession(
                session_id="other-empty-session",
                uid="user-2",
                profile_id=5,
                profile_override_id=6,
            ),
        ]
    )
    await db_session.commit()

    sessions = await message_crud.get_user_sessions(db_session, uid="user-1")

    assert len(sessions) == 1
    assert sessions[0].session_id == "empty-session"
    assert sessions[0].last_active is not None
    assert sessions[0].profile_id == 3
    assert sessions[0].profile_override_id == 4


@pytest.mark.asyncio
async def test_admin_user_sessions_include_messages_and_empty_sessions_for_all_users(db_session: AsyncSession):
    db_session.add_all(
        [
            User(uid="user-1", username="alice"),
            User(uid="user-2", username="bob"),
            ChatSession(session_id="active-session", uid="user-1"),
            ChatSession(session_id="empty-session", uid="user-2"),
            Message(
                session_id="active-session",
                uid="user-1",
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="hello",
                profile_id=1,
            ),
        ]
    )
    await db_session.commit()

    sessions = await message_crud.get_user_sessions(db_session, is_admin=True)

    assert {session.session_id for session in sessions} == {"active-session", "empty-session"}
    assert {session.uid for session in sessions} == {"user-1", "user-2"}


async def _seed_messages(db: AsyncSession) -> None:
    for message_id in range(1, 9):
        db.add(
            Message(
                id=message_id,
                uid="user-1" if message_id != 6 else "other-user",
                session_id="session-1" if message_id != 7 else "other-session",
                profile_id=1,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=f"message-{message_id}",
                is_processed=True,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_message_id_forward_cursor_pages_fixed_open_range(db_session: AsyncSession):
    await _seed_messages(db_session)

    first_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        limit=2,
    )
    second_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_after_id=first_page[-1].id,
        limit=2,
    )
    third_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_after_id=second_page[-1].id,
        limit=2,
    )

    assert [message.id for message in first_page] == [2, 3]
    assert [message.id for message in second_page] == [4, 5]
    assert [message.id for message in third_page] == []


@pytest.mark.asyncio
async def test_message_id_backward_cursor_pages_fixed_open_range(db_session: AsyncSession):
    await _seed_messages(db_session)

    first_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        limit=2,
    )
    second_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_before_id=first_page[-1].id,
        limit=2,
    )
    third_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_before_id=second_page[-1].id,
        limit=2,
    )

    assert [message.id for message in first_page] == [5, 4]
    assert [message.id for message in second_page] == [3, 2]
    assert [message.id for message in third_page] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 501])
async def test_message_id_cursor_rejects_unbounded_page_size(
    db_session: AsyncSession,
    limit: int,
):
    with pytest.raises(ValueError):
        await message_crud.get_history_forward_by_id(
            db_session,
            session_id="session-1",
            uid="user-1",
            limit=limit,
        )
    with pytest.raises(ValueError):
        await message_crud.get_history_backward_by_id(
            db_session,
            session_id="session-1",
            uid="user-1",
            limit=limit,
        )


def test_context_summary_storage_models_use_stable_work_and_stage_identity():
    stage_columns = ContextSummaryStage.__table__.columns
    fragment_columns = ContextSummaryFragment.__table__.columns

    assert "work_dedupe_key" in stage_columns
    assert "stage_key" in stage_columns
    assert "lower_stage_key" in stage_columns
    assert "expected_summary_revision" in stage_columns
    assert "work_dedupe_key" in fragment_columns
    assert "stage_key" in fragment_columns
    assert "dedupe_key" in fragment_columns
    assert ChatSession(session_id="session-1", uid="user-1").context_summary_revision == 0
