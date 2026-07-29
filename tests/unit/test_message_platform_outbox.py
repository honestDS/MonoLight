import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, update
from sqlmodel import select

from app.core.constants import (
    MSG_MESSAGE_PLATFORM_TOOL_USED,
    MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
)
from app.core.crud.message_platform_outbox import OUTBOX_LEASE_SECONDS, calculate_retry_delay_seconds, message_platform_outbox_crud
from app.core.i18n import t
from app.core.message_platforms import notifier as notifier_module
from app.core.message_platforms import outbound_text as outbound_text_module
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import OUTBOX_DELIVERY_TIMEOUT_SECONDS, MessagePlatformPollingManager
from app.core.message_platforms.notifier import build_outbox_dedupe_key, normalize_outbox_event
from app.core.message_platforms.outbound_text import OutboundTextPolicy, process_outbound_text_event, split_outbound_text_by_newline
from app.core.message_platforms.tool_output import combine_proactive_reply_tool_output
from app.core.utils.time import get_local_time
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.providers.database import AsyncSessionLocal, engine


class DeliveringHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"outbox-test"})
    sent_events: list[dict[str, Any]]

    def __init__(self, *, send_result: bool = True) -> None:
        self.send_result = send_result
        self.sent_events = []

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return False

    async def run(self, platform_id: int) -> None:
        return None

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        self.sent_events.append(event)
        return self.send_result


def _patch_outbound_text_message_persistence(monkeypatch):
    persisted_by_dedupe_key = {}
    user_save_calls = []
    assistant_save_calls = []

    async def save_refinement_prompt(
        *,
        db,
        session_id,
        uid,
        profile_id,
        refinement_prompt,
        dedupe_key,
    ):
        content = InternalMessage(role=MessageRole.USER, content=refinement_prompt)
        user_save_calls.append(
            {
                "session_id": session_id,
                "uid": uid,
                "role": MessageRole.USER,
                "msg_type": MessageType.TEXT,
                "content": content,
                "profile_id": profile_id,
                "is_processed": True,
                "dedupe_key": dedupe_key,
            }
        )
        saved = persisted_by_dedupe_key.get(dedupe_key)
        if saved is None:
            saved = InternalMessage(role=MessageRole.USER, content=refinement_prompt)
            persisted_by_dedupe_key[dedupe_key] = saved
        return saved

    async def get_by_dedupe_key(db, dedupe_key):
        return persisted_by_dedupe_key.get(dedupe_key)

    async def save_refinement_assistant_message(
        *,
        db,
        session_id,
        uid,
        profile_id,
        ai_msg,
        dedupe_key,
    ):
        assistant_save_calls.append(
            {
                "session_id": session_id,
                "uid": uid,
                "profile_id": profile_id,
                "ai_msg": ai_msg,
                "dedupe_key": dedupe_key,
            }
        )
        saved = persisted_by_dedupe_key.get(dedupe_key)
        if saved is None:
            saved = InternalMessage(role=MessageRole.ASSISTANT, content=ai_msg.content)
            persisted_by_dedupe_key[dedupe_key] = saved
        return saved

    monkeypatch.setattr(outbound_text_module, "_save_outbound_text_refinement_prompt", save_refinement_prompt)
    monkeypatch.setattr(outbound_text_module.message_crud, "get_by_dedupe_key", get_by_dedupe_key)
    monkeypatch.setattr(outbound_text_module, "_save_outbound_text_refinement_assistant_message", save_refinement_assistant_message)
    return {
        "persisted_by_dedupe_key": persisted_by_dedupe_key,
        "user_save_calls": user_save_calls,
        "assistant_save_calls": assistant_save_calls,
    }


@pytest.fixture(autouse=True)
async def clean_outbox_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: MessagePlatformOutbox.__table__.create(sync_connection, checkfirst=True))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagePlatformOutbox))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagePlatformOutbox))
        await db.commit()


@pytest.mark.asyncio
async def test_scheduled_event_uses_fixed_external_session_source(monkeypatch):
    session = type(
        "Session",
        (),
        {
            "source": "weixin-openclaw",
            "reply_target_source": "ws",
            "show_tool_calls": True,
        },
    )()
    enqueue_calls = []
    web_notify_calls = []

    class FakeDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_by_session_id(db, session_id):
        return session

    async def enqueue(db, **kwargs):
        enqueue_calls.append(kwargs)
        return type("OutboxItem", (), {"id": 7})(), True

    async def notify(*args, **kwargs):
        web_notify_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        notifier_module.session_crud,
        "get_by_session_id",
        get_by_session_id,
    )
    monkeypatch.setattr(
        notifier_module.message_platform_outbox_crud,
        "enqueue",
        enqueue,
    )
    monkeypatch.setattr(notifier_module.session_notifier, "notify", notify)

    event = {
        "event_id": "session-reply-work:59:event",
        "type": "proactive_reply",
        "source": "scheduled_task",
        "session_id": "weixin-openclaw:user-1",
        "work_id": 59,
        "trigger_message_id": 142,
        "content": "done",
    }
    await notifier_module.send_session_event(
        "uid-1",
        "weixin-openclaw:user-1",
        event,
    )

    assert web_notify_calls == []
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]["source"] == "weixin-openclaw"
    assert enqueue_calls[0]["event"] == event


def test_split_outbound_text_by_newline_prefers_closest_utf8_byte_balance():
    text = "测a\n测测\nabc"

    parts = split_outbound_text_by_newline(text, utf8_byte_limit=12)

    assert parts == ("测a", "测测\nabc")
    assert len(parts[0].encode("utf-8")) == 4
    assert len(parts[1].encode("utf-8")) == 10


@pytest.mark.asyncio
async def test_outbound_text_event_skips_refinement_when_original_content_can_split(monkeypatch):
    async def generate_reply(*args, **kwargs):
        raise AssertionError("refinement must not be called when the original text can split")

    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)
    event = {"type": "proactive_reply", "content": "a" * 6 + "\n" + "b" * 6}

    processed = await process_outbound_text_event(
        "uid",
        "session",
        "outbox-test",
        event,
        OutboundTextPolicy(
            utf8_byte_limit=10,
            max_refinement_attempts=3,
            additional_system_prompt="system prompt",
            refinement_prompt="refinement prompt",
            refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
            max_text_parts=2,
        ),
    )

    assert processed is not event
    assert processed == event
    assert set(processed) == {"type", "content"}


@pytest.mark.asyncio
async def test_outbound_text_refinement_stops_when_compressed_candidate_can_split(monkeypatch):
    submitted_candidates = []
    generated_calls = []
    persistence = _patch_outbound_text_message_persistence(monkeypatch)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(uid="uid", profile_id=1)

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=1, uid="uid")

    async def generate_reply(db, **kwargs):
        submitted_candidates.append(kwargs["submission_context"][0].content)
        generated_calls.append(kwargs)
        return InternalMessage(role=MessageRole.ASSISTANT, content="a" * 6 + "\n" + "b" * 6), [], []

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)

    processed = await process_outbound_text_event(
        "uid",
        "session",
        "outbox-test",
        {"type": "proactive_reply", "content": "a" * 20},
        OutboundTextPolicy(
            utf8_byte_limit=10,
            max_refinement_attempts=3,
            additional_system_prompt="system prompt",
            refinement_prompt="refinement prompt",
            refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
            max_text_parts=2,
        ),
    )

    assert submitted_candidates == ["a" * 20]
    assert processed == {"type": "proactive_reply", "content": "a" * 6 + "\n" + "b" * 6}
    assert generated_calls[0]["persist_response"] is False
    assert len(persistence["user_save_calls"]) == 1
    user_save_call = persistence["user_save_calls"][0]
    assert user_save_call["session_id"] == "session"
    assert user_save_call["uid"] == "uid"
    assert user_save_call["role"] == MessageRole.USER
    assert user_save_call["msg_type"] == MessageType.TEXT
    assert user_save_call["profile_id"] == 1
    assert user_save_call["is_processed"] is True
    assert len(user_save_call["dedupe_key"]) == 64
    assert user_save_call["content"].role == MessageRole.USER
    assert user_save_call["content"].content == "refinement prompt"
    assert len(persistence["assistant_save_calls"]) == 1
    assistant_save_call = persistence["assistant_save_calls"][0]
    assert assistant_save_call["session_id"] == "session"
    assert assistant_save_call["uid"] == "uid"
    assert assistant_save_call["profile_id"] == 1
    assert len(assistant_save_call["dedupe_key"]) == 64
    assert assistant_save_call["ai_msg"].role == MessageRole.ASSISTANT
    assert assistant_save_call["ai_msg"].content == "a" * 6 + "\n" + "b" * 6


@pytest.mark.asyncio
async def test_outbound_text_refinement_uses_the_previous_round_result(monkeypatch):
    submitted_candidates = []
    refinements = iter(["b" * 11, "c" * 10])
    _patch_outbound_text_message_persistence(monkeypatch)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        assert session_id == "session"
        return SimpleNamespace(uid="uid", profile_id=1)

    async def get_profile(db, profile_id):
        assert profile_id == 1
        return SimpleNamespace(id=1, uid="uid")

    async def generate_reply(db, **kwargs):
        submitted_candidates.append(kwargs["submission_context"][0].content)
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements)), [], []

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)

    event = {"type": "proactive_reply", "content": "a" * 12}
    processed = await process_outbound_text_event(
        "uid",
        "session",
        "outbox-test",
        event,
        OutboundTextPolicy(
            utf8_byte_limit=10,
            max_refinement_attempts=3,
            additional_system_prompt="system prompt",
            refinement_prompt="refinement prompt",
            refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
        ),
    )

    assert processed is not event
    assert event["content"] == "a" * 12
    assert processed["content"] == "c" * 10
    assert submitted_candidates == ["a" * 12, "b" * 11]


@pytest.mark.asyncio
async def test_outbound_text_refinement_keeps_candidate_after_unshortened_result(monkeypatch):
    submitted_candidates = []
    refinements = iter(["a" * 12, "b" * 10])
    _patch_outbound_text_message_persistence(monkeypatch)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(uid="uid", profile_id=1)

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=1, uid="uid")

    async def generate_reply(db, **kwargs):
        submitted_candidates.append(kwargs["submission_context"][0].content)
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements)), [], []

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)

    processed = await process_outbound_text_event(
        "uid",
        "session",
        "outbox-test",
        {"type": "proactive_reply", "content": "a" * 12},
        OutboundTextPolicy(
            utf8_byte_limit=10,
            max_refinement_attempts=3,
            additional_system_prompt="system prompt",
            refinement_prompt="refinement prompt",
            refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
        ),
    )

    assert processed["content"] == "b" * 10
    assert submitted_candidates == ["a" * 12, "a" * 12]


@pytest.mark.asyncio
async def test_outbound_text_refinement_uses_fallback_after_three_oversized_rounds(monkeypatch):
    submitted_candidates = []
    refinements = iter(["b" * 209, "c" * 208, "d" * 207])
    persistence = _patch_outbound_text_message_persistence(monkeypatch)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(uid="uid", profile_id=1)

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=1, uid="uid")

    async def generate_reply(db, **kwargs):
        submitted_candidates.append(kwargs["submission_context"][0].content)
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements)), [], []

    original_save_assistant_message = outbound_text_module._save_outbound_text_refinement_assistant_message

    async def save_assistant_message(*, db, session_id, uid, profile_id, ai_msg, dedupe_key):
        saved = await original_save_assistant_message(
            db=db,
            session_id=session_id,
            uid=uid,
            profile_id=profile_id,
            ai_msg=ai_msg,
            dedupe_key=dedupe_key,
        )
        if ai_msg.content == t(MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED):
            saved.content = "persisted fallback"
            persistence["persisted_by_dedupe_key"][dedupe_key] = saved
        return saved

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)
    monkeypatch.setattr(outbound_text_module, "_save_outbound_text_refinement_assistant_message", save_assistant_message)

    policy = OutboundTextPolicy(
        utf8_byte_limit=200,
        max_refinement_attempts=3,
        additional_system_prompt="system prompt",
        refinement_prompt="refinement prompt",
        refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
    )
    processed = await process_outbound_text_event(
        "uid",
        "session",
        "outbox-test",
        {"type": "proactive_reply", "content": "a" * 210},
        policy,
    )

    assert submitted_candidates == ["a" * 210, "b" * 209, "c" * 208]
    assert processed["content"] == "persisted fallback"
    assert len(processed["content"].encode("utf-8")) <= policy.utf8_byte_limit
    fallback_message = persistence["assistant_save_calls"][-1]["ai_msg"]
    assert fallback_message.role == MessageRole.ASSISTANT
    assert fallback_message.content == t(MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED)


@pytest.mark.asyncio
async def test_outbound_text_refinement_reuses_persisted_assistant_result_for_same_event(monkeypatch):
    generated_candidates = []
    persistence = _patch_outbound_text_message_persistence(monkeypatch)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(uid="uid", profile_id=1)

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=1, uid="uid")

    async def generate_reply(db, **kwargs):
        generated_candidates.append(kwargs["submission_context"][0].content)
        return InternalMessage(role=MessageRole.ASSISTANT, content="compressed"), [], []

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr("app.core.dispatcher.ChatDispatcher._generate_reply_from_history", generate_reply)

    event = {"event_id": "outbox:17", "type": "proactive_reply", "content": "a" * 20}
    policy = OutboundTextPolicy(
        utf8_byte_limit=10,
        max_refinement_attempts=3,
        additional_system_prompt="system prompt",
        refinement_prompt="refinement prompt",
        refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
    )

    first = await process_outbound_text_event("uid", "session", "outbox-test", event, policy)
    second = await process_outbound_text_event("uid", "session", "outbox-test", event, policy)

    assert first["content"] == "compressed"
    assert second["content"] == "compressed"
    assert generated_candidates == ["a" * 20]
    assert len(persistence["assistant_save_calls"]) == 1


@pytest.mark.asyncio
async def test_notifier_refines_event_before_enqueueing_outbox(monkeypatch):
    processing_order = []
    enqueued_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        assert session_id == "weixin-openclaw:user-1"
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=True)

    async def process_event(uid, session_id, source, event, policy):
        processing_order.append("process")
        assert (uid, session_id, source) == ("uid-1", "weixin-openclaw:user-1", "weixin-openclaw")
        return {**event, "content": "processed reply"}

    async def enqueue(db, **kwargs):
        processing_order.append("enqueue")
        enqueued_events.append(kwargs["event"])
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "process_outbound_text_event", process_event)
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)

    await notifier_module.send_session_event(
        "uid-1",
        "weixin-openclaw:user-1",
        {"type": "proactive_reply", "content": "oversized reply"},
    )

    assert processing_order == ["process", "enqueue"]
    assert enqueued_events == [{"type": "proactive_reply", "content": "processed reply"}]


@pytest.mark.asyncio
async def test_notifier_keeps_external_event_unchanged_when_tool_calls_are_hidden(monkeypatch):
    enqueued_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=False)

    async def enqueue(db, **kwargs):
        enqueued_events.append(kwargs["event"])
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "get_outbound_text_policy_registry", lambda: {})
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)
    event = {
        "event_id": "session-reply-work:1:event",
        "type": "proactive_reply",
        "content": "final reply",
        "files": [{"id": "file-1"}],
        "history": [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "name": "lookup", "arguments": "{}"}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        ],
    }

    await notifier_module.send_session_event("uid-1", "weixin-openclaw:user-1", event)

    assert enqueued_events == [event]


@pytest.mark.asyncio
async def test_notifier_enqueues_sanitized_stream_tool_summary_for_external_session(monkeypatch):
    enqueued_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        assert session_id == "weixin-openclaw:user-1"
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=True)

    async def enqueue(db, **kwargs):
        enqueued_events.append(kwargs["event"])
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "get_outbound_text_policy_registry", lambda: {})
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)
    event = {
        "type": "tool_start",
        "session_id": "weixin-openclaw:user-1",
        "work_id": 17,
        "event_sequence_no": 9,
        "content": " 我先检查 ",
        "tool_names": ["execute_shell", "read_text_file"],
        "arguments": {"command": "type C:\\secret.txt", "access_token": "sensitive-token"},
        "result": "sensitive tool result",
        "history": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "execute_shell",
                        "arguments": '{"command": "type C:\\\\secret.txt"}',
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "sensitive history result"},
        ],
        "tool_call_id": "call-1",
    }

    await notifier_module.send_session_stream_event("uid-1", "weixin-openclaw:user-1", event)

    assert len(enqueued_events) == 1
    enqueued_event = enqueued_events[0]
    assert enqueued_event["type"] == "proactive_reply"
    assert enqueued_event["content"] == (f"我先检查\n{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='execute_shell')}\n{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='read_text_file')}")
    assert enqueued_event["event_id"] == "stream_tool_call:17:9"
    assert enqueued_event["work_id"] == 17
    assert enqueued_event["session_id"] == "weixin-openclaw:user-1"
    serialized_event = json.dumps(enqueued_event)
    assert "arguments" not in serialized_event
    assert "result" not in serialized_event
    assert "history" not in serialized_event
    assert "tool_call_id" not in serialized_event


@pytest.mark.asyncio
async def test_notifier_does_not_enqueue_stream_tool_summary_when_tool_calls_are_hidden(monkeypatch):
    enqueue_calls = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        assert session_id == "weixin-openclaw:user-1"
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=False)

    async def enqueue(db, **kwargs):
        enqueue_calls.append(kwargs)
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "get_outbound_text_policy_registry", lambda: {})
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)

    await notifier_module.send_session_stream_event(
        "uid-1",
        "weixin-openclaw:user-1",
        {
            "type": "tool_start",
            "session_id": "weixin-openclaw:user-1",
            "work_id": 17,
            "event_sequence_no": 9,
            "content": " 我先检查 ",
            "tool_names": ["execute_shell", "read_text_file"],
        },
    )

    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_notifier_uses_stream_terminal_content_without_combining_tool_history(monkeypatch):
    enqueued_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        assert session_id == "weixin-openclaw:user-1"
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=True)

    async def enqueue(db, **kwargs):
        enqueued_events.append(kwargs["event"])
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "get_outbound_text_policy_registry", lambda: {})
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)
    event = {
        "event_id": "session-reply-work:17:event",
        "type": "proactive_reply",
        "session_id": "weixin-openclaw:user-1",
        "work_id": 17,
        "_stream_requested": True,
        "content": "最终回复",
        "files": [{"id": "file-1"}],
        "request_ids": ["request-1"],
        "history": [
            {
                "role": "assistant",
                "content": " 我先检查 ",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "execute_shell",
                        "arguments": '{"command": "type C:\\\\secret.txt"}',
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "sensitive tool result"},
        ],
    }

    await notifier_module.send_session_event("uid-1", "weixin-openclaw:user-1", event)

    assert len(enqueued_events) == 1
    enqueued_event = enqueued_events[0]
    assert enqueued_event["content"] == "最终回复"
    assert t(MSG_MESSAGE_PLATFORM_TOOL_USED, name="execute_shell") not in enqueued_event["content"]
    assert "history" not in enqueued_event
    assert "_stream_requested" not in enqueued_event
    assert enqueued_event["files"] == [{"id": "file-1"}]
    assert enqueued_event["request_ids"] == ["request-1"]
    assert enqueued_event["event_id"] == "session-reply-work:17:event"
    assert enqueued_event["work_id"] == 17
    assert enqueued_event["session_id"] == "weixin-openclaw:user-1"


@pytest.mark.asyncio
async def test_notifier_combines_tool_output_before_external_text_policy(monkeypatch):
    policy_events = []
    enqueued_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(source="weixin-openclaw", show_tool_calls=True)

    async def process_event(uid, session_id, source, event, policy):
        policy_events.append(event)
        return event

    async def enqueue(db, **kwargs):
        enqueued_events.append(kwargs["event"])
        return SimpleNamespace(id=7), True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module, "process_outbound_text_event", process_event)
    monkeypatch.setattr(notifier_module.message_platform_outbox_crud, "enqueue", enqueue)
    event = {
        "event_id": "session-reply-work:1:event",
        "type": "proactive_reply",
        "content": "final reply",
        "files": [{"id": "file-1"}],
        "history": [
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "arguments": '{"z": 1, "a": [2]}',
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        ],
    }

    await notifier_module.send_session_event("uid-1", "weixin-openclaw:user-1", event)

    assert len(policy_events) == 1
    content = policy_events[0]["content"]
    assert content == f"checking\n{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='lookup')}\n\nfinal reply"
    assert "history" not in policy_events[0]
    assert policy_events[0]["event_id"] == "session-reply-work:1:event"
    assert policy_events[0]["files"] == [{"id": "file-1"}]
    assert enqueued_events == policy_events
    assert "history" not in enqueued_events[0]
    serialized_event = json.dumps(enqueued_events[0])
    assert "arguments" not in serialized_event
    assert "tool result" not in serialized_event


@pytest.mark.asyncio
async def test_notifier_does_not_combine_web_event_tool_output(monkeypatch):
    notified_events = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_session(db, session_id):
        return SimpleNamespace(source="http", show_tool_calls=True)

    async def notify(uid, session_id, event, **kwargs):
        notified_events.append(event)
        return True

    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(notifier_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(notifier_module.session_notifier, "notify", notify)
    event = {
        "type": "proactive_reply",
        "content": "final reply",
        "history": [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "name": "lookup", "arguments": "{}"}],
            }
        ],
    }

    await notifier_module.send_session_event("uid-1", "session-1", event)

    assert notified_events == [event]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"type": "audit_confirmation", "plain_text": "confirm this"}', "confirm this"),
        ('{"type": "assistant_files", "text": "file reply", "files": [{"id": "file-1"}]}', "file reply"),
    ],
)
def test_combine_tool_output_uses_structured_final_reply_text(content, expected):
    combined = combine_proactive_reply_tool_output(
        {
            "event_id": "event-1",
            "type": "proactive_reply",
            "content": content,
            "files": [{"id": "file-1"}],
            "history": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "call-1", "name": "lookup"}],
                }
            ],
        }
    )

    assert combined["content"] == f"{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='lookup')}\n\n{expected}"
    assert "history" not in combined
    assert combined["event_id"] == "event-1"
    assert combined["files"] == [{"id": "file-1"}]


def test_combine_tool_output_lists_multiple_tools_without_round_content():
    combined = combine_proactive_reply_tool_output(
        {
            "event_id": "event-1",
            "type": "proactive_reply",
            "content": "final reply",
            "files": [{"id": "file-1"}],
            "history": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call-1", "name": "first"},
                        {"id": "call-2", "name": "second"},
                    ],
                }
            ],
        }
    )

    assert combined["content"] == (f"{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='first')}\n{t(MSG_MESSAGE_PLATFORM_TOOL_USED, name='second')}\n\nfinal reply")
    assert "history" not in combined
    assert combined["event_id"] == "event-1"
    assert combined["files"] == [{"id": "file-1"}]


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_by_dedupe_key():
    event = {"type": "proactive_reply", "task_id": 1, "content": "done"}
    dedupe_key = build_outbox_dedupe_key("uid", "session", "outbox-test", event)

    async with AsyncSessionLocal() as db:
        first, first_created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )
        second, second_created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id


@pytest.mark.asyncio
async def test_claim_is_atomic_and_requires_matching_owner_to_complete():
    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="claim-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )

    async with AsyncSessionLocal() as db:
        claimed = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-a")
    async with AsyncSessionLocal() as db:
        duplicate_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-b")
        wrong_owner_marked = await message_platform_outbox_crud.mark_sent(db, item_id=item.id, worker_id="worker-b")
        correct_owner_marked = await message_platform_outbox_crud.mark_sent(db, item_id=item.id, worker_id="worker-a")

    assert claimed is not None
    assert claimed.status == MessagePlatformOutboxStatus.PROCESSING
    assert claimed.attempt_count == 1
    assert duplicate_claim is None
    assert wrong_owner_marked is False
    assert correct_owner_marked is True


@pytest.mark.asyncio
async def test_expired_processing_item_can_be_reclaimed():
    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="expired-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )
        first_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-a")
        assert first_claim is not None
        await db.execute(update(MessagePlatformOutbox).where(MessagePlatformOutbox.id == item.id).values(lock_until=get_local_time() - timedelta(seconds=1)))
        await db.commit()

    async with AsyncSessionLocal() as db:
        second_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-b")

    assert second_claim is not None
    assert second_claim.locked_by == "worker-b"
    assert second_claim.attempt_count == 2


@pytest.mark.asyncio
async def test_manager_sends_and_marks_outbox_item_sent():
    handler = DeliveringHandler()
    manager = MessagePlatformPollingManager((handler,))
    event = {"type": "proactive_reply", "content": "done"}

    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="delivery-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )

    processed_count = await manager.process_outbox_batch()

    async with AsyncSessionLocal() as db:
        saved_item = await message_platform_outbox_crud.get(db, item.id)

    assert processed_count == 1
    assert handler.sent_events == [event]
    assert saved_item is not None
    assert saved_item.status == MessagePlatformOutboxStatus.SENT
    assert saved_item.sent_at is not None


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_terminal_items():
    now = get_local_time()
    expired_sent = MessagePlatformOutbox(
        dedupe_key="expired-sent",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.SENT,
        created_at=now - timedelta(days=10),
        sent_at=now - timedelta(days=8),
    )
    recent_sent = MessagePlatformOutbox(
        dedupe_key="recent-sent",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.SENT,
        sent_at=now,
    )
    expired_failed = MessagePlatformOutbox(
        dedupe_key="expired-failed",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.FAILED,
        created_at=now - timedelta(days=31),
    )
    pending = MessagePlatformOutbox(
        dedupe_key="old-pending",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.PENDING,
        created_at=now - timedelta(days=60),
    )
    async with AsyncSessionLocal() as db:
        db.add_all([expired_sent, recent_sent, expired_failed, pending])
        await db.commit()
        deleted_count = await message_platform_outbox_crud.cleanup_terminal_items(db)

    async with AsyncSessionLocal() as db:
        remaining_keys = {item.dedupe_key for item in (await db.execute(select(MessagePlatformOutbox))).scalars().all()}

    assert deleted_count == 2
    assert remaining_keys == {"recent-sent", "old-pending"}


@pytest.mark.asyncio
async def test_manager_requeues_failed_delivery_then_marks_terminal_failure():
    handler = DeliveringHandler(send_result=False)
    manager = MessagePlatformPollingManager((handler,))

    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="retry-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )

    await manager.process_outbox_batch()
    async with AsyncSessionLocal() as db:
        retried_item = await message_platform_outbox_crud.get(db, item.id)
        assert retried_item is not None
        assert retried_item.status == MessagePlatformOutboxStatus.PENDING
        assert retried_item.attempt_count == 1
        await db.execute(
            update(MessagePlatformOutbox)
            .where(MessagePlatformOutbox.id == item.id)
            .values(
                status=MessagePlatformOutboxStatus.PROCESSING,
                locked_by=manager._worker_id,
                attempt_count=5,
            )
        )
        await db.commit()
        terminal_item = await message_platform_outbox_crud.get(db, item.id)

    assert terminal_item is not None
    await manager._deliver_outbox_item(terminal_item)

    async with AsyncSessionLocal() as db:
        failed_item = await message_platform_outbox_crud.get(db, item.id)

    assert failed_item is not None
    assert failed_item.status == MessagePlatformOutboxStatus.FAILED
    assert failed_item.last_error


def test_retry_delay_uses_capped_exponential_backoff():
    assert [calculate_retry_delay_seconds(attempt) for attempt in (1, 2, 3, 4, 5, 10)] == [5, 10, 20, 40, 80, 300]


def test_outbox_lease_exceeds_delivery_timeout():
    assert OUTBOX_LEASE_SECONDS > OUTBOX_DELIVERY_TIMEOUT_SECONDS


def test_outbox_event_normalization_converts_non_json_values():
    timestamp = datetime(2026, 7, 10, tzinfo=UTC)

    normalized = normalize_outbox_event({"type": "proactive_reply", "created_at": timestamp})

    assert normalized == {"type": "proactive_reply", "created_at": "2026-07-10 00:00:00+00:00"}


def test_dedupe_key_is_stable_and_event_sensitive():
    event = {"type": "proactive_reply", "content": "done"}
    reordered_event = {"content": "done", "type": "proactive_reply"}
    changed_event = {**event, "content": "changed"}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", event)
    second = build_outbox_dedupe_key("uid", "session", "outbox-test", reordered_event)
    changed = build_outbox_dedupe_key("uid", "session", "outbox-test", changed_event)

    assert first == second
    assert first != changed


def test_dedupe_key_uses_stable_background_task_identity():
    first_event = {
        "type": "proactive_reply",
        "source": "background_task",
        "background_task_id": 42,
        "content": "first content",
    }
    repeated_event = {**first_event, "content": "regenerated content"}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", first_event)
    repeated = build_outbox_dedupe_key("uid", "session", "outbox-test", repeated_event)

    assert first == repeated


def test_dedupe_key_distinguishes_scheduled_task_runs():
    first_run = {
        "type": "proactive_reply",
        "source": "scheduled_task",
        "scheduled_task_id": 7,
        "trigger_message_id": 100,
    }
    second_run = {**first_run, "trigger_message_id": 101}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", first_run)
    second = build_outbox_dedupe_key("uid", "session", "outbox-test", second_run)

    assert first != second
