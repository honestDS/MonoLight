from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkStatus, SessionReplyWorkType
from tests.unit.response_test_support import llm_response_payload


def _background_result_event(**overrides):
    values = {
        "id": 50,
        "session_id": "session-1",
        "uid": "user-1",
        "profile_id": 3,
        "role": MessageRole.USER,
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
        sequence_no=1,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        source_type=SessionReplySourceType.BACKGROUND_TASK,
        dedupe_key="background-task:8",
        status=SessionReplyWorkStatus.RUNNING,
        execution_state={},
        created_at=datetime(2026, 7, 14, 0, 0, 0),
    )
    profile = SimpleNamespace(id=3, uid="user-1")
    result_event = _background_result_event()
    captured = {}
    interactive_response = llm_response_payload(
        "公开后台回复",
        history=[{"role": "assistant", "content": "公开历史", "created_at": 0.0}],
        files=[
            {
                "id": "file-1",
                "name": "result.txt",
                "mime_type": "text/plain",
                "size": 12,
                "download_url": "/files/file-1",
            }
        ],
    )

    async def get_task(_db, task_id):
        assert task_id == 8
        return task

    async def get_profile(_db, profile_id):
        assert profile_id == 3
        return profile

    async def get_result_event(_db, dedupe_key):
        assert dedupe_key == "background-task:8:result"
        return result_event

    async def dispatch_interactive_work(_db, **kwargs):
        captured.update(kwargs)
        return interactive_response

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module.message_crud, "get_by_dedupe_key", get_result_event)
    monkeypatch.setattr(executor_replies_module, "_dispatch_interactive_work", dispatch_interactive_work)

    response = await executor_replies_module._execute_background(object(), work)

    assert response == {
        "content": "公开后台回复",
        "history": [{"role": "assistant", "content": "公开历史", "created_at": 0.0}],
        "files": interactive_response["files"],
    }
    assert captured["message"] == result_event.content
    assert captured["message"] != task.result["output"]
    assert captured["initial_message"].role == MessageRole.USER
    assert captured["initial_message"].id == 50
    assert captured["history_before_id"] == 50
    assert captured["frozen_user_message_ids"] == [50]
    assert captured["allow_additional_user_messages"] is True
    assert captured["execution_resume_state"]["reload_history"] is True
    assert captured["execution_resume_state"]["context_summary_fixed_upper_message_id"] == 50
    assert "submission_context" not in captured
    assert "extra_messages" not in captured
    assert task.extra == legacy_extra


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_overrides", "event_overrides"),
    [
        pytest.param({}, None, id="missing-event"),
        pytest.param({}, {"id": None}, id="missing-event-id"),
        pytest.param({}, {"uid": "other-user"}, id="uid-mismatch"),
        pytest.param({}, {"session_id": "other-session"}, id="session-mismatch"),
        pytest.param({}, {"profile_id": 99}, id="profile-mismatch"),
        pytest.param({}, {"type": MessageType.TEXT}, id="type-mismatch"),
        pytest.param({"uid": "other-user"}, {}, id="task-uid-mismatch"),
        pytest.param({"session_id": "other-session"}, {}, id="task-session-mismatch"),
        pytest.param({"profile_id": 99}, {}, id="task-profile-mismatch"),
    ],
)
async def test_background_summary_rejects_invalid_persisted_result_boundary(monkeypatch, task_overrides, event_overrides):
    task_values = {
        "id": 8,
        "uid": "user-1",
        "session_id": "session-1",
        "profile_id": 3,
        "extra": {},
    }
    task_values.update(task_overrides)
    task = SimpleNamespace(**task_values)
    work = SimpleNamespace(
        id=12,
        sequence_no=1,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        source_type=SessionReplySourceType.BACKGROUND_TASK,
        status=SessionReplyWorkStatus.RUNNING,
        execution_state={},
    )
    profile = SimpleNamespace(id=3, uid="user-1")
    result_event = None if event_overrides is None else _background_result_event(**event_overrides)
    dispatch_called = False

    async def get_task(_db, task_id):
        assert task_id == 8
        return task

    async def get_profile(_db, profile_id):
        assert profile_id == 3
        return profile

    async def get_result_event(_db, dedupe_key):
        assert dedupe_key == "background-task:8:result"
        return result_event

    async def dispatch_interactive_work(*_args, **_kwargs):
        nonlocal dispatch_called
        dispatch_called = True
        return {}

    monkeypatch.setattr(executor_replies_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_replies_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_replies_module.message_crud, "get_by_dedupe_key", get_result_event)
    monkeypatch.setattr(executor_replies_module, "_dispatch_interactive_work", dispatch_interactive_work)

    with pytest.raises(RuntimeError):
        await executor_replies_module._execute_background(object(), work)

    assert dispatch_called is False
