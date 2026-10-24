from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_provider_usage import SessionReplyProviderRequestPurpose


def _background_result_event(**overrides):
    values = {
        "id": 50,
        "session_id": "session-1",
        "uid": "user-1",
        "profile_id": 3,
        "role": MessageRole.ASSISTANT,
        "type": MessageType.BACKGROUND_TASK_RESULT,
        "content": "persisted background result",
        "dedupe_key": "background-task:8:result",
    }
    values.update(overrides)
    return Message(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_extra",
    [
        pytest.param({}, id="missing-submission-context"),
        pytest.param({"submission_context": []}, id="empty-submission-context"),
        pytest.param(
            {"submission_context": [{"not_a_valid_internal_message": True}]},
            id="stale-invalid-submission-context",
        ),
    ],
)
async def test_background_summary_uses_persisted_result_event_boundary(monkeypatch, legacy_extra):
    task = SimpleNamespace(
        id=8,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_call_id="call-1",
        tool_name="execute_shell",
        status="succeeded",
        result={"status": "succeeded", "output": "stale task result"},
        error=None,
        extra=legacy_extra,
    )
    work = SimpleNamespace(
        id=12,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        dedupe_key="background-task:8",
        created_at=datetime(2026, 7, 14, 0, 0, 0),
    )
    profile = SimpleNamespace(id=3, uid="user-1")
    result_event = _background_result_event()
    captured = {}

    async def get_task(_db, task_id):
        assert task_id == 8
        return task

    async def get_profile(_db, profile_id):
        assert profile_id == 3
        return profile

    async def get_result_event(_db, dedupe_key):
        assert dedupe_key == "background-task:8:result"
        return result_event

    async def generate_reply(_db, work=None, **kwargs):
        captured.update(kwargs)
        return InternalMessage(role=MessageRole.ASSISTANT, content="后台总结"), [], [], None

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module.message_crud, "get_by_dedupe_key", get_result_event)
    monkeypatch.setattr(executor_replies_module, "_generate_auxiliary_reply_with_request_metadata", generate_reply)

    response = await executor_replies_module._execute_background(object(), work)

    assert response == {"content": "后台总结", "history": [], "files": []}
    assert captured["request_purpose"] == SessionReplyProviderRequestPurpose.BACKGROUND_SUMMARY
    assert captured["history_before_id"] == 51
    assert captured["initial_fixed_upper_message_id"] == 50
    assert "submission_context" not in captured
    assert "extra_messages" not in captured
    assert task.extra == legacy_extra


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_overrides",
    [
        pytest.param(None, id="missing-event"),
        pytest.param({"id": None}, id="missing-event-id"),
        pytest.param({"uid": "other-user"}, id="uid-mismatch"),
        pytest.param({"session_id": "other-session"}, id="session-mismatch"),
        pytest.param({"profile_id": 99}, id="profile-mismatch"),
        pytest.param({"type": MessageType.TEXT}, id="type-mismatch"),
    ],
)
async def test_background_summary_rejects_invalid_persisted_result_event(monkeypatch, event_overrides):
    task = SimpleNamespace(
        id=8,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        extra={},
    )
    work = SimpleNamespace(
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
    )
    profile = SimpleNamespace(id=3, uid="user-1")
    result_event = None if event_overrides is None else _background_result_event(**event_overrides)
    model_called = False

    async def get_task(_db, task_id):
        assert task_id == 8
        return task

    async def get_profile(_db, profile_id):
        assert profile_id == 3
        return profile

    async def get_result_event(_db, dedupe_key):
        assert dedupe_key == "background-task:8:result"
        return result_event

    async def generate_reply(_db, **kwargs):
        nonlocal model_called
        model_called = True
        return InternalMessage(role=MessageRole.ASSISTANT, content="不应生成"), [], [], None

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module.message_crud, "get_by_dedupe_key", get_result_event)
    monkeypatch.setattr(executor_replies_module, "_generate_auxiliary_reply_with_request_metadata", generate_reply)

    with pytest.raises(RuntimeError):
        await executor_replies_module._execute_background(object(), work)

    assert model_called is False
