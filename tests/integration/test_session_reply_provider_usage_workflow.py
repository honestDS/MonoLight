import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.core.constants import ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMException
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.core.utils.request_token_baseline import build_provider_request_usage_metadata
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_provider_usage import SessionReplyProviderRequestPurpose, SessionReplyProviderUsage
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.database_support import clone_sqlite_schema

SESSION_ID = "session-provider-usage"
UID = "user-1"
PROFILE_ID = 1


@pytest_asyncio.fixture
async def session_factory(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "session-reply-provider-usage.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(
        database_path,
        tables=[
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplyProviderUsage.__table__,
            SessionReplyStreamEvent.__table__,
        ],
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(Profile(id=PROFILE_ID, uid=UID, name="provider usage", configs={}))
            await db.commit()
        yield factory
    finally:
        await engine.dispose()


def _session_metadata(total_input_tokens: int, total_cached_tokens: int, total_output_tokens: int) -> dict[str, Any]:
    input_tokens = total_input_tokens
    return {
        "model_id": "test-model",
        "protocol": "OPENAI",
        "input_tokens": input_tokens,
        "input_tokens_source": "provider" if input_tokens else "estimated",
        "cached_tokens": total_cached_tokens,
        "output_tokens": total_output_tokens,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        "total_input_tokens": total_input_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_output_tokens": total_output_tokens,
        "cache_hit_rate": total_cached_tokens / total_input_tokens if total_input_tokens else 0.0,
    }


def _request_metadata(
    request_id: str,
    *,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    include_totals: bool = False,
) -> dict[str, Any]:
    provider_metrics = {
        "input_tokens": input_tokens,
        "input_tokens_source": "provider",
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
    }
    metadata = {
        "type": "llm_request_metadata",
        "model_id": "test-model",
        "protocol": "OPENAI",
        "input_tokens": input_tokens,
        "input_tokens_source": "provider",
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        **build_provider_request_usage_metadata(request_id, provider_metrics),
    }
    if include_totals:
        metadata.update(
            total_input_tokens=input_tokens,
            total_cached_tokens=cached_tokens,
            total_output_tokens=output_tokens,
            cache_hit_rate=cached_tokens / input_tokens if input_tokens else 0.0,
        )
    return metadata


def _work(
    *,
    work_id: int,
    sequence_no: int,
    work_type: SessionReplyWorkType,
    source_type: SessionReplySourceType,
    source_id: str,
) -> SessionReplyWorkItem:
    return SessionReplyWorkItem(
        id=work_id,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        sequence_no=sequence_no,
        work_type=work_type,
        source_type=source_type,
        source_id=source_id,
        dedupe_key=f"provider-usage-work:{work_id}",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[1],
        execution_state={"stream_requested": False},
    )


async def _seed_session_and_works(
    factory: async_sessionmaker[AsyncSession],
    *,
    metadata: dict[str, Any],
    works: list[SessionReplyWorkItem],
) -> None:
    async with factory() as db:
        db.add(
            ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=PROFILE_ID,
                llm_request_metadata=metadata,
            )
        )
        db.add_all(works)
        await db.commit()


@pytest.mark.asyncio
async def test_auxiliary_usage_is_audit_only_while_foreground_usage_updates_session_totals(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_session_and_works(
        session_factory,
        metadata=_session_metadata(1000, 250, 200),
        works=[
            _work(
                work_id=7,
                sequence_no=1,
                work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
                source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                source_id="6",
            ),
            _work(
                work_id=8,
                sequence_no=2,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id="1",
            ),
        ],
    )
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    async def generate_reply(_db, **kwargs):
        await kwargs["request_metadata_callback"](
            _request_metadata(
                "auxiliary-request",
                input_tokens=120,
                cached_tokens=20,
                output_tokens=7,
            )
        )
        return InternalMessage(role=MessageRole.ASSISTANT, content="auxiliary reply"), [], []

    monkeypatch.setattr(executor_metadata_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)

    async with session_factory() as db:
        scheduled_work = await db.get(SessionReplyWorkItem, 7)
        assert scheduled_work is not None
        response = await executor_replies_module._execute_scheduled(db, scheduled_work, "worker-1")
    assert response["content"] == "auxiliary reply"

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(SessionReplyProviderUsage))).scalars().all())
        assert len(usages) == 1
        assert usages[0].provider_request_id == "auxiliary-request"
        assert usages[0].work_id == 7
        assert (usages[0].input_tokens, usages[0].cached_tokens, usages[0].output_tokens) == (120, 20, 7)
        assert session.llm_request_metadata["total_input_tokens"] == 1000
        assert session.llm_request_metadata["total_cached_tokens"] == 250
        assert session.llm_request_metadata["total_output_tokens"] == 200
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(0.25)

    async def dispatch(**kwargs):
        await kwargs["request_metadata_callback"](
            _request_metadata(
                "foreground-request",
                input_tokens=300,
                cached_tokens=90,
                output_tokens=11,
            )
        )
        return {"choices": []}

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)

    async with session_factory() as db:
        foreground_work = await db.get(SessionReplyWorkItem, 8)
        assert foreground_work is not None
        response = await executor_interactive_module._dispatch_interactive_work(
            db,
            work=foreground_work,
            worker_id="worker-1",
            message="foreground input",
            initial_message=InternalMessage(id=1, role=MessageRole.USER, content="foreground input"),
            history_before_id=1,
            frozen_user_message_ids=[1],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
        )
    assert response == {"choices": []}

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(SessionReplyProviderUsage))).scalars().all())
        usage_by_request_id = {usage.provider_request_id: usage for usage in usages}
        assert len(usages) == 2
        assert {request_id: (usage.work_id, usage.input_tokens, usage.cached_tokens, usage.output_tokens) for request_id, usage in usage_by_request_id.items()} == {
            "auxiliary-request": (7, 120, 20, 7),
            "foreground-request": (8, 300, 90, 11),
        }
        assert usage_by_request_id["auxiliary-request"].request_purpose == SessionReplyProviderRequestPurpose.SCHEDULED_SUMMARY
        assert usage_by_request_id["foreground-request"].request_purpose == SessionReplyProviderRequestPurpose.MAIN_DIALOGUE
        assert session.llm_request_metadata["total_input_tokens"] == 1300
        assert session.llm_request_metadata["total_cached_tokens"] == 340
        assert session.llm_request_metadata["total_output_tokens"] == 211
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(340 / 1300)
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)


@pytest.mark.asyncio
async def test_provider_usage_survives_reply_rollback_cancellation_and_retry_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_session_and_works(
        session_factory,
        metadata=_session_metadata(0, 0, 0),
        works=[
            _work(
                work_id=7,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id="1",
            )
        ],
    )
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    request_attempts = [
        ("provider-request-1", "empty"),
        ("provider-request-1", "empty"),
        ("provider-request-2", "empty"),
        ("provider-request-3", "cancelled"),
    ]

    async def dispatch(**kwargs):
        request_id, outcome = request_attempts.pop(0)
        await kwargs["request_metadata_callback"](
            _request_metadata(
                request_id,
                input_tokens=100,
                cached_tokens=100,
                output_tokens=0,
                include_totals=True,
            )
        )
        if outcome == "cancelled":
            raise asyncio.CancelledError
        raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)

    async def run_attempt(*, cancelled: bool = False) -> None:
        async with session_factory() as reply_db:
            work = (await reply_db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == 7))).scalar_one()
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await executor_interactive_module._dispatch_interactive_work(
                        reply_db,
                        work=work,
                        worker_id="worker-1",
                        message="original",
                        initial_message=InternalMessage(id=1, role=MessageRole.USER, content="original"),
                        history_before_id=1,
                        frozen_user_message_ids=[1],
                        attachments=None,
                        allow_additional_user_messages=False,
                        execution_resume_state=None,
                    )
            else:
                with pytest.raises(LLMException):
                    await executor_interactive_module._dispatch_interactive_work(
                        reply_db,
                        work=work,
                        worker_id="worker-1",
                        message="original",
                        initial_message=InternalMessage(id=1, role=MessageRole.USER, content="original"),
                        history_before_id=1,
                        frozen_user_message_ids=[1],
                        attachments=None,
                        allow_additional_user_messages=False,
                        execution_resume_state=None,
                    )
            await reply_db.rollback()

    async def read_state() -> tuple[dict[str, Any], list[SessionReplyProviderUsage]]:
        async with session_factory() as db:
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
            usages = list((await db.execute(select(SessionReplyProviderUsage).order_by(SessionReplyProviderUsage.provider_request_id))).scalars().all())
            return dict(session.llm_request_metadata or {}), usages

    await run_attempt()
    metadata, usages = await read_state()
    assert metadata["total_input_tokens"] == 100
    assert metadata["total_cached_tokens"] == 100
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 1
    assert usages[0].provider_request_id == "provider-request-1"
    assert usages[0].work_id == 7
    assert all(not key.startswith("_provider_") for key in metadata)

    await run_attempt()
    metadata, usages = await read_state()
    assert metadata["total_input_tokens"] == 100
    assert metadata["total_cached_tokens"] == 100
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 1

    await run_attempt()
    metadata, usages = await read_state()
    assert metadata["total_input_tokens"] == 200
    assert metadata["total_cached_tokens"] == 200
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 2
    assert {usage.provider_request_id for usage in usages} == {
        "provider-request-1",
        "provider-request-2",
    }

    await run_attempt(cancelled=True)
    metadata, usages = await read_state()
    assert metadata["total_input_tokens"] == 300
    assert metadata["total_cached_tokens"] == 300
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 3
    assert {usage.provider_request_id for usage in usages} == {
        "provider-request-1",
        "provider-request-2",
        "provider-request-3",
    }
