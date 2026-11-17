from types import SimpleNamespace

import pytest

from app.core.constants import MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED
from app.core.i18n import t
from app.core.message_platforms import outbound_text as outbound_text_module
from app.core.message_platforms.outbound_text import OutboundTextPolicy, process_outbound_text_event, split_outbound_text_by_newline
from app.models.message import InternalMessage, MessageRole, MessageType
from tests.integration.message_platforms import test_message_platform_outbox as outbox_module
from tests.integration.message_platforms.test_message_platform_outbox import _patch_outbound_text_message_persistence

clean_outbox_table = outbox_module.clean_outbox_table


def test_split_outbound_text_by_newline_prefers_closest_utf8_byte_balance():
    text = "测a\n测测\nabc"

    parts = split_outbound_text_by_newline(text, utf8_byte_limit=12)

    assert parts == ("测a", "测测\nabc")
    assert len(parts[0].encode("utf-8")) == 4
    assert len(parts[1].encode("utf-8")) == 10


@pytest.mark.asyncio
async def test_outbound_text_event_skips_refinement_when_original_content_can_split(monkeypatch):
    async def generate_refinement(*args, **kwargs):
        raise AssertionError("refinement must not be called when the original text can split")

    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)
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

    async def generate_refinement(db, **kwargs):
        submitted_candidates.append(kwargs["candidate"])
        generated_calls.append(kwargs)
        return InternalMessage(role=MessageRole.ASSISTANT, content="a" * 6 + "\n" + "b" * 6)

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)

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
    assert generated_calls[0]["candidate"] == "a" * 20
    assert generated_calls[0]["policy"].utf8_byte_limit == 10
    assert len(persistence["user_save_calls"]) == 1
    user_save_call = persistence["user_save_calls"][0]
    assert user_save_call["session_id"] == "session"
    assert user_save_call["uid"] == "uid"
    assert user_save_call["role"] == MessageRole.USER
    assert user_save_call["msg_type"] == MessageType.OUTBOUND_TEXT_REFINEMENT
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

    async def generate_refinement(db, **kwargs):
        submitted_candidates.append(kwargs["candidate"])
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements))

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)

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

    async def generate_refinement(db, **kwargs):
        submitted_candidates.append(kwargs["candidate"])
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements))

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)

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

    async def generate_refinement(db, **kwargs):
        submitted_candidates.append(kwargs["candidate"])
        return InternalMessage(role=MessageRole.ASSISTANT, content=next(refinements))

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
    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)
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

    async def generate_refinement(db, **kwargs):
        generated_candidates.append(kwargs["candidate"])
        return InternalMessage(role=MessageRole.ASSISTANT, content="compressed")

    monkeypatch.setattr(outbound_text_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(outbound_text_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(outbound_text_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(outbound_text_module, "_generate_refinement_message", generate_refinement)

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
