import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.core.constants import ERR_LLM_EMPTY_RESPONSE
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.dispatchers import stream as stream_dispatcher_module
from app.core.exceptions import LLMException
from app.core.provider_usage import build_reply_usage_snapshot
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.request_token_baseline import (
    build_provider_request_usage_metadata,
    extract_confirmed_provider_input_tokens,
    extract_provider_token_metrics,
)
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord
from app.models.message import InternalMessage, Message, MessageRole
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.provider_usage import ProviderUsageDaily, ProviderUsageDetail, ProviderUsageType
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemSetting
from app.transformers.openai import OpenAIChatCompletionsTransformer
from tests.database_support import clone_sqlite_schema
from tests.unit.response_test_support import llm_response_payload

SESSION_ID = "session-provider-usage"
UID = "user-1"
PROFILE_ID = 1
_NON_MAIN_METADATA_PRIVATE_KEYS = frozenset(
    {
        "input_tokens",
        "input_tokens_source",
        "context_window_tokens",
        "max_output_tokens",
        "output_tokens",
        "cached_tokens",
        "channel_id",
        "model_id",
        "protocol",
        "context_summary_revision",
        "context_content_revision",
    }
)


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
            Message.__table__,
            AuditRecord.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplySequence.__table__,
            ProviderUsageDetail.__table__,
            ProviderUsageDaily.__table__,
            SystemSetting.__table__,
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
        "cache_hit_rate": total_cached_tokens / total_input_tokens if total_input_tokens else None,
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
        "_provider_channel_key": "test-channel-key",
        "_provider_channel_name": "test-channel",
        "_provider_response_model_id": "test-response-model",
        "_provider_usage_observed": True,
        "_provider_token_usage": {
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
        },
        "_provider_image_count": None,
        **build_provider_request_usage_metadata(request_id, provider_metrics),
    }
    if include_totals:
        metadata.update(
            total_input_tokens=input_tokens,
            total_cached_tokens=cached_tokens,
            total_output_tokens=output_tokens,
            cache_hit_rate=cached_tokens / input_tokens if input_tokens else None,
        )
    return metadata


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if not (isinstance(key, str) and key.startswith("_provider_"))}


def _assert_non_main_metadata_boundary(metadata: dict[str, Any]) -> None:
    assert _NON_MAIN_METADATA_PRIVATE_KEYS.isdisjoint(metadata)
    assert "summary_trigger_tokens" not in metadata
    assert not any(key.startswith("_provider_") for key in metadata)


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
    metadata: dict[str, Any] | None,
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
                work_type=SessionReplyWorkType.AGENT_WORK,
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
        ai_msg, turn_messages, files, _ = await executor_metadata_module._generate_auxiliary_reply_with_request_metadata(
            db,
            work=scheduled_work,
            request_purpose=ProviderUsageType.SCHEDULED_SUMMARY,
            uid=scheduled_work.uid,
            session_id=scheduled_work.session_id,
            profile=await db.get(Profile, scheduled_work.profile_id),
            call_context="session_reply_scheduled_summary",
            allow_tools=True,
            initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            initial_fixed_upper_message_id=int(scheduled_work.source_id),
            restrict_tools_to_background_allowlist=False,
            reply_source="scheduled_task",
            final_message_dedupe_key="session-reply-work:7:result",
        )
        response = {"content": ai_msg.content}
    assert response["content"] == "auxiliary reply"

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        scheduled_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(usages) == 1
        assert scheduled_usages == []
        assert usages[0].provider_request_id == "auxiliary-request"
        assert usages[0].work_id == 7
        assert usages[0].type == ProviderUsageType.SCHEDULED_SUMMARY.value
        assert (usages[0].input_tokens, usages[0].cached_tokens, usages[0].output_tokens) == (120, 20, 7)
        assert (
            usages[0].channel_key,
            usages[0].channel_name,
            usages[0].response_model_id,
            usages[0].protocol,
        ) == ("test-channel-key", "test-channel", "test-response-model", "openai")
        daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily) == 1
        assert (
            daily[0].type,
            daily[0].input_tokens,
            daily[0].cached_tokens,
            daily[0].output_tokens,
            daily[0].request_count,
        ) == (ProviderUsageType.SCHEDULED_SUMMARY.value, 120, 20, 7, 1)
        assert session.llm_request_metadata["total_input_tokens"] == 1000
        assert session.llm_request_metadata["total_cached_tokens"] == 250
        assert session.llm_request_metadata["total_output_tokens"] == 200
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(0.25)

    foreground_model_id = "foreground-model-" + "m" * 300
    foreground_response_model_id = "foreground-response-model-" + "r" * 300

    async def dispatch(**kwargs):
        foreground_metadata = _request_metadata(
            "foreground-request",
            input_tokens=300,
            cached_tokens=90,
            output_tokens=11,
        )
        foreground_metadata.update(
            model_id=foreground_model_id,
            _provider_response_model_id=foreground_response_model_id,
        )
        await kwargs["request_metadata_callback"](foreground_metadata)
        return llm_response_payload("foreground reply")

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
    assert response["choices"][0]["message"]["content"] == "foreground reply"
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert response["choices"][0]["finish_reason"] is True

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        usage_by_request_id = {usage.provider_request_id: usage for usage in usages}
        assert len(usages) == 2
        assert {request_id: (usage.work_id, usage.input_tokens, usage.cached_tokens, usage.output_tokens) for request_id, usage in usage_by_request_id.items()} == {
            "auxiliary-request": (7, 120, 20, 7),
            "foreground-request": (8, 300, 90, 11),
        }
        assert usage_by_request_id["auxiliary-request"].type == ProviderUsageType.SCHEDULED_SUMMARY.value
        assert usage_by_request_id["foreground-request"].type == ProviderUsageType.MAIN_DIALOGUE.value
        daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert {row.type: (row.input_tokens, row.cached_tokens, row.output_tokens, row.request_count) for row in daily} == {
            ProviderUsageType.MAIN_DIALOGUE.value: (300, 90, 11, 1),
            ProviderUsageType.SCHEDULED_SUMMARY.value: (120, 20, 7, 1),
        }
        foreground_usage = usage_by_request_id["foreground-request"]
        main_dialogue_daily = next(row for row in daily if row.type == ProviderUsageType.MAIN_DIALOGUE.value)
        assert (
            foreground_usage.model_id,
            foreground_usage.response_model_id,
            main_dialogue_daily.model_id,
            main_dialogue_daily.response_model_id,
        ) == (
            foreground_model_id,
            foreground_response_model_id,
            foreground_model_id,
            foreground_response_model_id,
        )
        assert session.llm_request_metadata["total_input_tokens"] == 1300
        assert session.llm_request_metadata["total_cached_tokens"] == 340
        assert session.llm_request_metadata["total_output_tokens"] == 211
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(340 / 1300)
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)


@pytest.mark.parametrize(
    ("usage", "expected_detail", "expected_daily", "expected_totals", "expected_cache_hit_rate", "expected_context_input_tokens"),
    [
        pytest.param(
            None,
            (None, None, None),
            (0, 0, 0, 1, 0, 0, 0),
            (1000, 250, 200),
            0.25,
            1000,
            id="usage-missing",
        ),
        pytest.param(
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
            (0, 0, 0),
            (0, 0, 0, 1, 1, 1, 1),
            (1000, 250, 200),
            0.25,
            1000,
            id="usage-all-zero",
        ),
        pytest.param(
            {"completion_tokens": 5},
            (None, None, 5),
            (0, 0, 5, 1, 0, 0, 1),
            # Output-only usage updates the cumulative output without replacing context.
            (1000, 250, 205),
            0.25,
            1000,
            id="usage-completion-only",
        ),
        pytest.param(
            {"prompt_tokens": 20},
            (20, None, None),
            (20, 0, 0, 1, 1, 0, 0),
            (1020, 250, 200),
            250 / 1020,
            20,
            id="usage-input-only",
        ),
        pytest.param(
            {
                "prompt_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
            (20, 0, None),
            (20, 0, 0, 1, 1, 1, 0),
            (1020, 250, 200),
            250 / 1020,
            20,
            id="usage-zero-cache",
        ),
        pytest.param(
            {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
            (20, 3, 5),
            (20, 3, 5, 1, 1, 1, 1),
            (1020, 253, 205),
            253 / 1020,
            20,
            id="usage-full",
        ),
    ],
)
@pytest.mark.asyncio
async def test_foreground_reply_real_response_snapshot_is_separate_from_session_totals(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, Any] | None,
    expected_detail: tuple[int | None, int | None, int | None],
    expected_daily: tuple[int, int, int, int, int, int, int],
    expected_totals: tuple[int, int, int],
    expected_cache_hit_rate: float,
    expected_context_input_tokens: int,
) -> None:
    work = _work(
        work_id=7,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
    )
    await _seed_session_and_works(
        session_factory,
        metadata=_session_metadata(1000, 250, 200),
        works=[work],
    )
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)

    raw_payload: dict[str, Any] = {
        "model": "test-response-model",
        "choices": [{"message": {"role": "assistant", "content": "foreground reply"}}],
    }
    if usage is not None:
        raw_payload["usage"] = usage
    response = OpenAIChatCompletionsTransformer.to_internal_response(raw_payload, default_model="test-model")
    provider_metrics = extract_provider_token_metrics(response.usage)
    channel = SimpleNamespace(id=1, usage_key="test-channel-key", name="test-channel")
    request_metadata = {
        "type": "llm_request_metadata",
        "channel_id": channel.id,
        "model_id": "test-model",
        "protocol": "OPENAI",
        "input_tokens": 12,
        "input_tokens_source": "estimated",
        "context_window_tokens": 4096,
        "max_output_tokens": 512,
        **provider_metrics,
        **build_provider_request_usage_metadata("foreground-real-response-request", provider_metrics),
        **build_reply_usage_snapshot(channel, response),
    }

    await executor_metadata_module._persist_interactive_work_request_metadata(request_metadata, work=work)
    await executor_metadata_module._persist_interactive_work_request_metadata(request_metadata, work=work)

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        details = list((await db.execute(select(ProviderUsageDetail))).scalars().all())
        daily_rows = list((await db.execute(select(ProviderUsageDaily))).scalars().all())

    assert len(details) == 1
    assert len(daily_rows) == 1
    detail = details[0]
    daily = daily_rows[0]
    assert detail.type == ProviderUsageType.MAIN_DIALOGUE.value
    assert detail.provider_request_id == "foreground-real-response-request"
    assert detail.work_id == work.id
    assert (detail.input_tokens, detail.cached_tokens, detail.output_tokens) == expected_detail
    assert (
        daily.input_tokens,
        daily.cached_tokens,
        daily.output_tokens,
        daily.request_count,
        daily.input_tokens_reported_count,
        daily.cached_tokens_reported_count,
        daily.output_tokens_reported_count,
    ) == expected_daily
    assert (
        session.llm_request_metadata["total_input_tokens"],
        session.llm_request_metadata["total_cached_tokens"],
        session.llm_request_metadata["total_output_tokens"],
    ) == expected_totals
    assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(expected_cache_hit_rate)
    assert session.llm_request_metadata["input_tokens"] == expected_context_input_tokens
    assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)


@pytest.mark.parametrize("scheduled_sequence_no", [1, 3])
@pytest.mark.asyncio
async def test_scheduled_interactive_usage_stays_outside_session_totals_and_survives_work_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    scheduled_sequence_no: int,
) -> None:
    main_metadata = _session_metadata(100000, 250, 200)
    main_metadata.update(
        {
            "channel_id": 1,
            "context_summary_revision": 0,
            "context_content_revision": 0,
            "input_tokens": 50000,
            "work_sequence_no": 2,
            "event_sequence_no": 4,
        }
    )
    await _seed_session_and_works(
        session_factory,
        metadata=main_metadata,
        works=[
            _work(
                work_id=7,
                sequence_no=scheduled_sequence_no,
                work_type=SessionReplyWorkType.AGENT_WORK,
                source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                source_id="6",
            )
        ],
    )
    async with session_factory() as db:
        session = await db.get(ChatSession, SESSION_ID)
        assert session is not None
        session.llm_request_metadata_work_sequence_no = 2
        session.llm_request_metadata_event_sequence_no = 4
        await db.commit()

    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    request_metadata = _request_metadata(
        "scheduled-main-request",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    request_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )

    async def dispatch(**kwargs):
        assert kwargs["execution_scope"] == "task"
        await kwargs["request_metadata_callback"](request_metadata)
        await kwargs["request_metadata_callback"](request_metadata)
        response = llm_response_payload("scheduled reply")
        response["llm_request_metadata"] = dict(request_metadata)
        return response

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)

    async with session_factory() as db:
        scheduled_work = await db.get(SessionReplyWorkItem, 7)
        assert scheduled_work is not None
        response = await executor_interactive_module._dispatch_interactive_work(
            db,
            work=scheduled_work,
            worker_id="worker-1",
            message="scheduled input",
            initial_message=InternalMessage(id=6, role=MessageRole.USER, content="scheduled input"),
            history_before_id=6,
            frozen_user_message_ids=[6],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
            execution_scope="task",
            frozen_context_messages=[],
        )
    assert response["choices"][0]["message"]["content"] == "scheduled reply"
    assert "llm_request_metadata" not in response

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        scheduled_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        ordinary_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(scheduled_usages) == 1
        usage = scheduled_usages[0]
        assert usage.provider_request_id == "scheduled-main-request"
        assert usage.work_id == 7
        assert usage.type == ProviderUsageType.SCHEDULED_TASK.value
        assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (120, 20, 7)
        assert (
            usage.channel_key,
            usage.channel_name,
            usage.response_model_id,
            usage.protocol,
        ) == ("test-channel-key", "test-channel", "test-response-model", "openai")
        assert ordinary_usages == []
        daily_scheduled = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        daily_ordinary = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily_scheduled) == 1
        assert daily_ordinary == []
        assert (
            daily_scheduled[0].type,
            daily_scheduled[0].input_tokens,
            daily_scheduled[0].cached_tokens,
            daily_scheduled[0].output_tokens,
            daily_scheduled[0].request_count,
        ) == (ProviderUsageType.SCHEDULED_TASK.value, 120, 20, 7, 1)
        assert session.llm_request_metadata == main_metadata

        scheduled_work = await db.get(SessionReplyWorkItem, 7)
        assert scheduled_work is not None
        scheduled_work.status = SessionReplyWorkStatus.SUCCEEDED
        scheduled_work.updated_at = get_local_time() - timedelta(hours=25)
        db.add(SessionReplySequence(session_id=SESSION_ID, next_sequence_no=scheduled_sequence_no + 1))
        await db.commit()

        cleanup_result = await session_reply_work_item_crud.cleanup_terminal_items(db)
        assert cleanup_result.work_items == 1
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == 7))).scalar_one_or_none() is None
        assert (await db.execute(select(SessionReplySequence).where(SessionReplySequence.session_id == SESSION_ID))).scalar_one_or_none() is not None

        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        assert session.llm_request_metadata["total_input_tokens"] == 100000
        assert session.llm_request_metadata["total_cached_tokens"] == 250
        assert session.llm_request_metadata["total_output_tokens"] == 200
        remaining_usage = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(remaining_usage) == 1
        assert (remaining_usage[0].input_tokens, remaining_usage[0].cached_tokens, remaining_usage[0].output_tokens) == (120, 20, 7)
        remaining_daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(remaining_daily) == 1
        assert (remaining_daily[0].input_tokens, remaining_daily[0].cached_tokens, remaining_daily[0].output_tokens) == (120, 20, 7)

        assert await session_crud.delete_by_session(db, session_id=SESSION_ID, uid=UID) == 1
        assert (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one_or_none() is None
        assert (await db.execute(select(SessionReplySequence).where(SessionReplySequence.session_id == SESSION_ID))).scalar_one_or_none() is None
        details_after_session_delete = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        daily_after_session_delete = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(details_after_session_delete) == 1
        assert details_after_session_delete[0].provider_request_id == "scheduled-main-request"
        assert len(daily_after_session_delete) == 1
        assert (daily_after_session_delete[0].input_tokens, daily_after_session_delete[0].cached_tokens, daily_after_session_delete[0].output_tokens) == (120, 20, 7)


@pytest.mark.parametrize(
    ("work_type", "source_type", "source_id", "frozen_context_messages", "updates_context"),
    [
        pytest.param(
            SessionReplyWorkType.FOREGROUND_REPLY,
            SessionReplySourceType.USER_MESSAGE,
            "1",
            [],
            False,
            id="foreground-frozen-context",
        ),
        pytest.param(
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            SessionReplySourceType.AUDIT_RECORD,
            "999",
            [],
            False,
            id="confirmed-tool-frozen-context",
        ),
        pytest.param(
            SessionReplyWorkType.BACKGROUND_TASK_RESUME,
            SessionReplySourceType.BACKGROUND_TASK,
            "999",
            [],
            False,
            id="background-resume-frozen-context",
        ),
        pytest.param(
            SessionReplyWorkType.FOREGROUND_REPLY,
            SessionReplySourceType.USER_MESSAGE,
            "1",
            None,
            True,
            id="foreground-main-context",
        ),
    ],
)
@pytest.mark.parametrize("stream_requested", [False, True], ids=["non-stream", "stream"])
@pytest.mark.asyncio
async def test_interactive_metadata_boundary_is_cumulative_without_leaking_non_main_context(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    work_type: SessionReplyWorkType,
    source_type: SessionReplySourceType,
    source_id: str,
    frozen_context_messages: list[InternalMessage] | None,
    updates_context: bool,
    stream_requested: bool,
) -> None:
    main_metadata = _session_metadata(1000, 250, 200)
    main_metadata.update(
        {
            "input_tokens": 777,
            "input_tokens_source": "provider",
            "cached_tokens": 77,
            "output_tokens": 33,
            "context_window_tokens": 8192,
            "max_output_tokens": 1024,
            "channel_id": 41,
            "model_id": "main-model",
            "protocol": "MAIN_PROTOCOL",
            "context_summary_revision": 5,
            "context_content_revision": 6,
            "main_context_marker": "unchanged",
            "work_sequence_no": 2,
            "event_sequence_no": 4,
        }
    )
    await _seed_session_and_works(
        session_factory,
        metadata=main_metadata,
        works=[
            _work(
                work_id=7,
                sequence_no=9,
                work_type=work_type,
                source_type=source_type,
                source_id=source_id,
            )
        ],
    )
    async with session_factory() as db:
        session = await db.get(ChatSession, SESSION_ID)
        work = await db.get(SessionReplyWorkItem, 7)
        assert session is not None
        assert work is not None
        session.llm_request_metadata_work_sequence_no = 2
        session.llm_request_metadata_event_sequence_no = 4
        work.execution_state = {"stream_requested": stream_requested}
        await db.commit()

    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    request_metadata = _request_metadata(
        "boundary-request",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    request_metadata.update(
        channel_id=2,
        context_summary_revision=7,
        context_content_revision=8,
    )

    def build_response() -> dict[str, Any]:
        response = llm_response_payload("boundary reply")
        response["llm_request_metadata"] = _public_metadata(request_metadata)
        return response

    async def dispatch(**kwargs):
        await kwargs["request_metadata_callback"](dict(request_metadata))
        await kwargs["request_metadata_callback"](dict(request_metadata))
        return build_response()

    async def dispatch_stream(**kwargs):
        await kwargs["request_metadata_callback"](dict(request_metadata))
        await kwargs["request_metadata_callback"](dict(request_metadata))
        yield {"type": "llm_request_metadata", **_public_metadata(request_metadata)}
        yield {"type": "llm_request_metadata", **_public_metadata(request_metadata)}
        yield {"type": "done", "response": build_response(), "response_id": "boundary-response"}

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch_stream", dispatch_stream)

    async with session_factory() as db:
        work = await db.get(SessionReplyWorkItem, 7)
        assert work is not None
        response = await executor_interactive_module._dispatch_interactive_work(
            db,
            work=work,
            worker_id="worker-1",
            message="boundary input",
            initial_message=InternalMessage(id=1, role=MessageRole.USER, content="boundary input"),
            history_before_id=1,
            frozen_user_message_ids=[1],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
            frozen_context_messages=frozen_context_messages,
        )

    response_metadata = response.get("llm_request_metadata")
    assert isinstance(response_metadata, dict)
    assert response_metadata["total_input_tokens"] == 1120
    assert response_metadata["total_cached_tokens"] == 270
    assert response_metadata["total_output_tokens"] == 207
    assert response_metadata["cache_hit_rate"] == pytest.approx(270 / 1120)
    assert response_metadata["work_id"] == 7
    assert response_metadata["work_sequence_no"] == 9
    if updates_context:
        assert response_metadata["input_tokens"] == 120
        assert response_metadata["context_window_tokens"] == 4096
        assert response_metadata["max_output_tokens"] == 512
    else:
        _assert_non_main_metadata_boundary(response_metadata)
    assert not any(key.startswith("_provider_") for key in response_metadata)

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        stream_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == 7).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all())

        assert len(usages) == 1
        usage = usages[0]
        assert usage.provider_request_id == "boundary-request"
        assert usage.work_id == 7
        assert usage.type == ProviderUsageType.MAIN_DIALOGUE.value
        assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (120, 20, 7)
        assert (
            usage.channel_key,
            usage.channel_name,
            usage.response_model_id,
            usage.protocol,
        ) == ("test-channel-key", "test-channel", "test-response-model", "openai")
        daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily) == 1
        assert (
            daily[0].type,
            daily[0].input_tokens,
            daily[0].cached_tokens,
            daily[0].output_tokens,
            daily[0].request_count,
        ) == (ProviderUsageType.MAIN_DIALOGUE.value, 120, 20, 7, 1)

        assert session.llm_request_metadata is not None
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)
        assert session.llm_request_metadata["total_input_tokens"] == 1120
        assert session.llm_request_metadata["total_cached_tokens"] == 270
        assert session.llm_request_metadata["total_output_tokens"] == 207
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(270 / 1120)
        if updates_context:
            assert session.llm_request_metadata["input_tokens"] == 120
            assert session.llm_request_metadata["context_window_tokens"] == 4096
            assert session.llm_request_metadata["max_output_tokens"] == 512
            assert session.llm_request_metadata_work_sequence_no == 9
            assert session.llm_request_metadata_event_sequence_no == (2 if stream_requested else 0)
        else:
            for key in (
                "input_tokens",
                "input_tokens_source",
                "cached_tokens",
                "output_tokens",
                "context_window_tokens",
                "max_output_tokens",
                "channel_id",
                "model_id",
                "protocol",
                "context_summary_revision",
                "context_content_revision",
                "main_context_marker",
                "work_sequence_no",
                "event_sequence_no",
            ):
                assert session.llm_request_metadata[key] == main_metadata[key]
            assert "summary_trigger_tokens" not in session.llm_request_metadata
            assert session.llm_request_metadata_work_sequence_no == 2
            assert session.llm_request_metadata_event_sequence_no == 4

        if stream_requested:
            assert len(stream_events) == 2
            assert [item.sequence_no for item in stream_events] == [1, 2]
            for expected_sequence_no, stream_item in enumerate(stream_events, start=1):
                metadata_event = stream_item.event
                assert stream_item.work_id == 7
                assert metadata_event["type"] == "llm_request_metadata"
                assert metadata_event["session_id"] == SESSION_ID
                assert metadata_event["work_id"] == 7
                assert metadata_event["work_sequence_no"] == 9
                assert metadata_event["event_sequence_no"] == expected_sequence_no
                assert metadata_event["event_sequence_no"] == stream_item.sequence_no
                assert metadata_event["total_input_tokens"] == 1120
                assert metadata_event["total_cached_tokens"] == 270
                assert metadata_event["total_output_tokens"] == 207
                assert metadata_event["cache_hit_rate"] == pytest.approx(270 / 1120)
                assert not any(key.startswith("_provider_") for key in metadata_event)
                if updates_context:
                    assert metadata_event["input_tokens"] == 120
                    assert metadata_event["context_window_tokens"] == 4096
                    assert metadata_event["max_output_tokens"] == 512
                else:
                    _assert_non_main_metadata_boundary(metadata_event)
        else:
            assert not stream_events


@pytest.mark.parametrize("stream_requested", [False, True])
@pytest.mark.asyncio
async def test_interleaved_foreground_and_scheduled_usage_keep_latest_totals(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    stream_requested: bool,
) -> None:
    main_metadata = _session_metadata(1000, 250, 200)
    main_metadata.update(
        {
            "channel_id": 1,
            "context_summary_revision": 0,
            "context_content_revision": 0,
            "work_sequence_no": 1,
            "event_sequence_no": 0,
        }
    )
    await _seed_session_and_works(
        session_factory,
        metadata=main_metadata,
        works=[
            _work(
                work_id=7,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id="1",
            ),
            _work(
                work_id=8,
                sequence_no=2,
                work_type=SessionReplyWorkType.AGENT_WORK,
                source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                source_id="6",
            ),
        ],
    )
    async with session_factory() as db:
        session = await db.get(ChatSession, SESSION_ID)
        foreground_work = await db.get(SessionReplyWorkItem, 7)
        assert session is not None
        assert foreground_work is not None
        session.llm_request_metadata_work_sequence_no = 1
        session.llm_request_metadata_event_sequence_no = 0
        foreground_work.execution_state = {"stream_requested": stream_requested}
        await db.commit()

    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    foreground_metadata = _request_metadata(
        "foreground-interleaved-request",
        input_tokens=300,
        cached_tokens=90,
        output_tokens=11,
    )
    foreground_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )
    foreground_stale_metadata = {
        **foreground_metadata,
        "total_input_tokens": 1300,
        "total_cached_tokens": 340,
        "total_output_tokens": 211,
        "cache_hit_rate": 340 / 1300,
    }
    scheduled_metadata = _request_metadata(
        "scheduled-interleaved-request",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    scheduled_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )
    foreground_callback_finished = asyncio.Event()
    scheduled_returned = asyncio.Event()

    async def emit_foreground_response(request_metadata_callback, stream_event_callback):
        await request_metadata_callback(foreground_metadata)
        foreground_callback_finished.set()
        await scheduled_returned.wait()
        if stream_event_callback is not None:
            await stream_event_callback(_public_metadata(foreground_stale_metadata))
        response = llm_response_payload("foreground reply")
        response["llm_request_metadata"] = _public_metadata(foreground_stale_metadata)
        return response

    async def dispatch(**kwargs):
        if kwargs["execution_scope"] == "task":
            await foreground_callback_finished.wait()
            await kwargs["request_metadata_callback"](scheduled_metadata)
            response = llm_response_payload("scheduled reply")
            response["llm_request_metadata"] = _public_metadata(scheduled_metadata)
            return response
        return await emit_foreground_response(
            kwargs["request_metadata_callback"],
            kwargs.get("stream_event_callback"),
        )

    async def dispatch_interactive(**kwargs):
        return await emit_foreground_response(
            kwargs["request_metadata_callback"],
            kwargs["stream_event_callback"],
        )

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)
    if stream_requested:
        monkeypatch.setattr(stream_dispatcher_module, "dispatch_interactive", dispatch_interactive)

    async def run_work(work_id: int, *, scheduled: bool):
        async with session_factory() as db:
            work = await db.get(SessionReplyWorkItem, work_id)
            assert work is not None
            response = await executor_interactive_module._dispatch_interactive_work(
                db,
                work=work,
                worker_id="worker-1",
                message="scheduled input" if scheduled else "foreground input",
                initial_message=InternalMessage(
                    id=6 if scheduled else 1,
                    role=MessageRole.USER,
                    content="scheduled input" if scheduled else "foreground input",
                ),
                history_before_id=6 if scheduled else 1,
                frozen_user_message_ids=[6] if scheduled else [1],
                attachments=None,
                allow_additional_user_messages=False,
                execution_resume_state=None,
                execution_scope="task" if scheduled else None,
                frozen_context_messages=[] if scheduled else None,
            )
        if scheduled:
            scheduled_returned.set()
        return response

    foreground_task = asyncio.create_task(run_work(7, scheduled=False))
    scheduled_task = asyncio.create_task(run_work(8, scheduled=True))
    foreground_response, scheduled_response = await asyncio.wait_for(
        asyncio.gather(foreground_task, scheduled_task),
        timeout=10,
    )

    assert foreground_response["choices"][0]["message"]["content"] == "foreground reply"
    assert scheduled_response["choices"][0]["message"]["content"] == "scheduled reply"
    assert "llm_request_metadata" not in scheduled_response

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        ordinary_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        scheduled_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(ordinary_usages) == 1
        assert {usage.provider_request_id: (usage.work_id, usage.input_tokens, usage.cached_tokens, usage.output_tokens) for usage in ordinary_usages} == {
            "foreground-interleaved-request": (7, 300, 90, 11),
        }
        assert ordinary_usages[0].type == ProviderUsageType.MAIN_DIALOGUE.value
        assert len(scheduled_usages) == 1
        assert (
            scheduled_usages[0].provider_request_id,
            scheduled_usages[0].work_id,
            scheduled_usages[0].input_tokens,
            scheduled_usages[0].cached_tokens,
            scheduled_usages[0].output_tokens,
        ) == ("scheduled-interleaved-request", 8, 120, 20, 7)
        assert scheduled_usages[0].type == ProviderUsageType.SCHEDULED_TASK.value
        assert (
            ordinary_usages[0].channel_key,
            ordinary_usages[0].channel_name,
            ordinary_usages[0].response_model_id,
            ordinary_usages[0].protocol,
        ) == ("test-channel-key", "test-channel", "test-response-model", "openai")
        daily_ordinary = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        daily_scheduled = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily_ordinary) == 1
        assert len(daily_scheduled) == 1
        assert (
            daily_ordinary[0].type,
            daily_ordinary[0].input_tokens,
            daily_ordinary[0].cached_tokens,
            daily_ordinary[0].output_tokens,
            daily_ordinary[0].request_count,
        ) == (ProviderUsageType.MAIN_DIALOGUE.value, 300, 90, 11, 1)
        assert (
            daily_scheduled[0].type,
            daily_scheduled[0].input_tokens,
            daily_scheduled[0].cached_tokens,
            daily_scheduled[0].output_tokens,
            daily_scheduled[0].request_count,
        ) == (ProviderUsageType.SCHEDULED_TASK.value, 120, 20, 7, 1)
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)
        assert session.llm_request_metadata["total_input_tokens"] == 1300
        assert session.llm_request_metadata["total_cached_tokens"] == 340
        assert session.llm_request_metadata["total_output_tokens"] == 211
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(340 / 1300)
        assert session.llm_request_metadata["input_tokens"] == 300
        assert session.llm_request_metadata_work_sequence_no == 1
        assert (
            extract_confirmed_provider_input_tokens(
                session.llm_request_metadata,
                channel_id=1,
                model_id="test-model",
                protocol="OPENAI",
                context_summary_revision=0,
                context_content_revision=0,
            )
            == 300
        )
        if stream_requested:
            stream_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == 7).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all())
            metadata_events = [event.event for event in stream_events if event.event.get("type") == "llm_request_metadata"]
            assert metadata_events
            assert session.llm_request_metadata_event_sequence_no == metadata_events[-1]["event_sequence_no"]
            assert all(event["total_input_tokens"] == 1300 for event in metadata_events)
            assert all(event["total_cached_tokens"] == 340 for event in metadata_events)
            assert all(event["total_output_tokens"] == 211 for event in metadata_events)
            assert all(event["cache_hit_rate"] == pytest.approx(340 / 1300) for event in metadata_events)
            assert all(not any(key.startswith("_provider_") for key in event) for event in metadata_events)
        else:
            assert session.llm_request_metadata_event_sequence_no == 0


@pytest.mark.asyncio
async def test_scheduled_interactive_usage_without_chat_stats_does_not_create_session_usage(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_session_and_works(
        session_factory,
        metadata=None,
        works=[
            _work(
                work_id=7,
                sequence_no=99,
                work_type=SessionReplyWorkType.AGENT_WORK,
                source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                source_id="6",
            )
        ],
    )
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)

    request_metadata = _request_metadata(
        "scheduled-without-baseline",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    request_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )

    async def dispatch(**kwargs):
        await kwargs["request_metadata_callback"](request_metadata)
        response = llm_response_payload("scheduled reply")
        response["llm_request_metadata"] = dict(request_metadata)
        return response

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)

    async with session_factory() as db:
        scheduled_work = await db.get(SessionReplyWorkItem, 7)
        assert scheduled_work is not None
        response = await executor_interactive_module._dispatch_interactive_work(
            db,
            work=scheduled_work,
            worker_id="worker-1",
            message="scheduled input",
            initial_message=InternalMessage(id=6, role=MessageRole.USER, content="scheduled input"),
            history_before_id=6,
            frozen_user_message_ids=[6],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
            execution_scope="task",
            frozen_context_messages=[],
        )
    assert response["choices"][0]["message"]["content"] == "scheduled reply"
    assert "llm_request_metadata" not in response

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        scheduled_usage = (await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().one()
        assert (await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all() == []
        assert scheduled_usage.type == ProviderUsageType.SCHEDULED_TASK.value
        assert (scheduled_usage.input_tokens, scheduled_usage.cached_tokens, scheduled_usage.output_tokens) == (120, 20, 7)
        scheduled_daily = (await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().one()
        assert (
            scheduled_daily.input_tokens,
            scheduled_daily.cached_tokens,
            scheduled_daily.output_tokens,
            scheduled_daily.request_count,
        ) == (120, 20, 7, 1)
        assert (await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all() == []
        assert session.llm_request_metadata is None


@pytest.mark.asyncio
async def test_provider_usage_waits_for_main_metadata_snapshot_before_committing_totals(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_metadata = _session_metadata(1300, 340, 211)
    main_metadata.update(
        {
            "channel_id": 1,
            "context_summary_revision": 0,
            "context_content_revision": 0,
            "input_tokens": 300,
            "work_sequence_no": 1,
            "event_sequence_no": 0,
        }
    )
    await _seed_session_and_works(
        session_factory,
        metadata=main_metadata,
        works=[
            _work(
                work_id=7,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id="1",
            ),
            _work(
                work_id=8,
                sequence_no=2,
                work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
                source_type=SessionReplySourceType.BACKGROUND_TASK,
                source_id="6",
            ),
        ],
    )
    async with session_factory() as db:
        session = await db.get(ChatSession, SESSION_ID)
        background_resume_work = await db.get(SessionReplyWorkItem, 8)
        assert session is not None
        assert background_resume_work is not None
        session.llm_request_metadata_work_sequence_no = 1
        session.llm_request_metadata_event_sequence_no = 0
        await db.commit()

    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    background_resume_metadata = _request_metadata(
        "background-resume-lock-request",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    background_resume_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )
    main_request_metadata = _request_metadata(
        "foreground-lock-request",
        input_tokens=300,
        cached_tokens=90,
        output_tokens=11,
    )
    main_request_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
        total_input_tokens=1300,
        total_cached_tokens=340,
        total_output_tokens=211,
        cache_hit_rate=340 / 1300,
        request_purpose=ProviderUsageType.MAIN_DIALOGUE.value,
        work_sequence_no=1,
        event_sequence_no=0,
    )

    provider_task = None
    async with session_factory() as reply_db:
        try:
            locked_session = await session_crud.get_by_session_id_for_update(
                reply_db,
                session_id=SESSION_ID,
                uid=UID,
            )
            assert locked_session is not None
            provider_task = asyncio.create_task(
                executor_metadata_module._persist_interactive_work_request_metadata(
                    background_resume_metadata,
                    work=background_resume_work,
                    update_session_context=False,
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(provider_task), timeout=0.2)

            updated = await session_crud.update_llm_request_metadata(
                reply_db,
                session_id=SESSION_ID,
                uid=UID,
                metadata=main_request_metadata,
                commit=True,
            )
            assert updated is True
            provider_result = await asyncio.wait_for(asyncio.shield(provider_task), timeout=5)
            assert provider_result is not None
            assert (
                provider_result.total_input_tokens,
                provider_result.total_cached_tokens,
                provider_result.total_output_tokens,
            ) == (1420, 360, 218)
        finally:
            if reply_db.in_transaction():
                await reply_db.rollback()
            if provider_task is not None:
                if not provider_task.done():
                    provider_task.cancel()
                await asyncio.gather(provider_task, return_exceptions=True)

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        scheduled_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(usages) == 1
        assert scheduled_usages == []
        usage = usages[0]
        assert usage.provider_request_id == "background-resume-lock-request"
        assert usage.work_id == 8
        assert usage.type == ProviderUsageType.MAIN_DIALOGUE.value
        assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (120, 20, 7)
        daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily) == 1
        assert (
            daily[0].type,
            daily[0].input_tokens,
            daily[0].cached_tokens,
            daily[0].output_tokens,
            daily[0].request_count,
        ) == (ProviderUsageType.MAIN_DIALOGUE.value, 120, 20, 7, 1)
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)
        assert session.llm_request_metadata["total_input_tokens"] == 1420
        assert session.llm_request_metadata["total_cached_tokens"] == 360
        assert session.llm_request_metadata["total_output_tokens"] == 218
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(360 / 1420)
        assert session.llm_request_metadata["input_tokens"] == 300
        assert session.llm_request_metadata_work_sequence_no == 1
        assert session.llm_request_metadata_event_sequence_no == 0


@pytest.mark.asyncio
async def test_foreground_usage_updates_baseline_after_scheduled_usage_with_larger_sequence(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_session_and_works(
        session_factory,
        metadata=None,
        works=[
            _work(
                work_id=7,
                sequence_no=99,
                work_type=SessionReplyWorkType.AGENT_WORK,
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

    scheduled_metadata = _request_metadata(
        "scheduled-before-foreground",
        input_tokens=120,
        cached_tokens=20,
        output_tokens=7,
    )
    scheduled_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )
    foreground_metadata = _request_metadata(
        "foreground-after-scheduled",
        input_tokens=300,
        cached_tokens=90,
        output_tokens=11,
    )
    foreground_metadata.update(
        channel_id=1,
        context_summary_revision=0,
        context_content_revision=0,
    )

    async def dispatch(**kwargs):
        request_metadata = scheduled_metadata if kwargs["execution_scope"] == "task" else foreground_metadata
        await kwargs["request_metadata_callback"](request_metadata)
        response = llm_response_payload("scheduled reply" if kwargs["execution_scope"] == "task" else "foreground reply")
        response["llm_request_metadata"] = _public_metadata(request_metadata)
        return response

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", dispatch)

    async with session_factory() as db:
        scheduled_work = await db.get(SessionReplyWorkItem, 7)
        assert scheduled_work is not None
        await executor_interactive_module._dispatch_interactive_work(
            db,
            work=scheduled_work,
            worker_id="worker-1",
            message="scheduled input",
            initial_message=InternalMessage(id=6, role=MessageRole.USER, content="scheduled input"),
            history_before_id=6,
            frozen_user_message_ids=[6],
            attachments=None,
            allow_additional_user_messages=False,
            execution_resume_state=None,
            execution_scope="task",
            frozen_context_messages=[],
        )

    async with session_factory() as db:
        foreground_work = await db.get(SessionReplyWorkItem, 8)
        assert foreground_work is not None
        await executor_interactive_module._dispatch_interactive_work(
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

    async with session_factory() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
        ordinary_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        scheduled_usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(ordinary_usages) == 1
        assert ordinary_usages[0].provider_request_id == "foreground-after-scheduled"
        assert ordinary_usages[0].type == ProviderUsageType.MAIN_DIALOGUE.value
        assert len(scheduled_usages) == 1
        scheduled_usage = scheduled_usages[0]
        assert scheduled_usage.provider_request_id == "scheduled-before-foreground"
        assert scheduled_usage.type == ProviderUsageType.SCHEDULED_TASK.value
        daily_ordinary = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        daily_scheduled = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type == ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
        assert len(daily_ordinary) == 1
        assert len(daily_scheduled) == 1
        assert (
            daily_ordinary[0].type,
            daily_ordinary[0].input_tokens,
            daily_ordinary[0].cached_tokens,
            daily_ordinary[0].output_tokens,
            daily_ordinary[0].request_count,
        ) == (ProviderUsageType.MAIN_DIALOGUE.value, 300, 90, 11, 1)
        assert (
            daily_scheduled[0].type,
            daily_scheduled[0].input_tokens,
            daily_scheduled[0].cached_tokens,
            daily_scheduled[0].output_tokens,
            daily_scheduled[0].request_count,
        ) == (ProviderUsageType.SCHEDULED_TASK.value, 120, 20, 7, 1)
        assert not any(key.startswith("_provider_") for key in session.llm_request_metadata)
        assert session.llm_request_metadata["input_tokens"] == 300
        assert session.llm_request_metadata["input_tokens_source"] == "provider"
        assert session.llm_request_metadata["channel_id"] == 1
        assert session.llm_request_metadata["model_id"] == "test-model"
        assert session.llm_request_metadata["protocol"] == "OPENAI"
        assert session.llm_request_metadata["context_summary_revision"] == 0
        assert session.llm_request_metadata["context_content_revision"] == 0
        assert session.llm_request_metadata["total_input_tokens"] == 300
        assert session.llm_request_metadata["total_cached_tokens"] == 90
        assert session.llm_request_metadata["total_output_tokens"] == 11
        assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(90 / 300)
        assert session.llm_request_metadata_work_sequence_no == 2
        assert session.llm_request_metadata_event_sequence_no == 0
        assert (
            extract_confirmed_provider_input_tokens(
                session.llm_request_metadata,
                channel_id=1,
                model_id="test-model",
                protocol="OPENAI",
                context_summary_revision=0,
                context_content_revision=0,
            )
            == 300
        )


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

    async def read_state() -> tuple[dict[str, Any], list[ProviderUsageDetail], list[ProviderUsageDaily]]:
        async with session_factory() as db:
            session = (await db.execute(select(ChatSession).where(ChatSession.session_id == SESSION_ID))).scalar_one()
            usages = list((await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value).order_by(ProviderUsageDetail.provider_request_id))).scalars().all())
            daily = list((await db.execute(select(ProviderUsageDaily).where(ProviderUsageDaily.type != ProviderUsageType.SCHEDULED_TASK.value))).scalars().all())
            return dict(session.llm_request_metadata or {}), usages, daily

    await run_attempt()
    metadata, usages, daily = await read_state()
    assert metadata["total_input_tokens"] == 100
    assert metadata["total_cached_tokens"] == 100
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 1
    assert usages[0].provider_request_id == "provider-request-1"
    assert usages[0].work_id == 7
    assert usages[0].type == ProviderUsageType.MAIN_DIALOGUE.value
    assert (
        daily[0].type,
        daily[0].input_tokens,
        daily[0].cached_tokens,
        daily[0].output_tokens,
        daily[0].request_count,
    ) == (ProviderUsageType.MAIN_DIALOGUE.value, 100, 100, 0, 1)
    assert all(not key.startswith("_provider_") for key in metadata)

    await run_attempt()
    metadata, usages, daily = await read_state()
    assert metadata["total_input_tokens"] == 100
    assert metadata["total_cached_tokens"] == 100
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 1
    assert (
        daily[0].input_tokens,
        daily[0].cached_tokens,
        daily[0].output_tokens,
        daily[0].request_count,
    ) == (100, 100, 0, 1)

    await run_attempt()
    metadata, usages, daily = await read_state()
    assert metadata["total_input_tokens"] == 200
    assert metadata["total_cached_tokens"] == 200
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 2
    assert {usage.provider_request_id for usage in usages} == {
        "provider-request-1",
        "provider-request-2",
    }
    assert (
        daily[0].input_tokens,
        daily[0].cached_tokens,
        daily[0].output_tokens,
        daily[0].request_count,
    ) == (200, 200, 0, 2)

    await run_attempt(cancelled=True)
    metadata, usages, daily = await read_state()
    assert metadata["total_input_tokens"] == 300
    assert metadata["total_cached_tokens"] == 300
    assert metadata["cache_hit_rate"] == 1
    assert len(usages) == 3
    assert {usage.provider_request_id for usage in usages} == {
        "provider-request-1",
        "provider-request-2",
        "provider-request-3",
    }
    assert (
        daily[0].input_tokens,
        daily[0].cached_tokens,
        daily[0].output_tokens,
        daily[0].request_count,
    ) == (300, 300, 0, 3)
