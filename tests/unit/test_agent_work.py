from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.constants import END_SESSION_SCOPE_FOREGROUND, END_SESSION_SCOPE_TASK
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.session_reply_queue import executor_agent_work as executor_agent_work_module
from app.core.tools.agent_work import CreateArguments, SendArguments
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message, MessageRole, MessageType


@pytest.mark.parametrize("missing_field", ["parent_work_id", "subagent_id", "parent_tool_call_id"])
def test_agent_work_spec_requires_parent_for_parent_report_target(missing_field: str) -> None:
    parent_fields = {
        "parent_work_id": 17,
        "subagent_id": "a" * 32,
        "parent_tool_call_id": "call-parent",
    }
    missing_parent_fields = parent_fields.copy()
    missing_parent_fields.pop(missing_field)

    with pytest.raises(ValidationError):
        AgentWorkSpec(
            instruction="inspect the build",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.PARENT_WORK,
            **missing_parent_fields,
        )

    spec = AgentWorkSpec(
        instruction="inspect the build",
        execution_scope=END_SESSION_SCOPE_TASK,
        report_target=AgentWorkReportTarget.PARENT_WORK,
        **parent_fields,
    )

    assert spec.parent_work_id == 17
    assert spec.subagent_id == "a" * 32
    assert spec.parent_tool_call_id == "call-parent"


def test_agent_work_spec_rejects_parent_fields_for_session() -> None:
    with pytest.raises(ValidationError):
        AgentWorkSpec(
            instruction="inspect the build",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
            parent_work_id=17,
            subagent_id="a" * 32,
            parent_tool_call_id="call-parent",
        )


@pytest.mark.parametrize(
    "trigger_content",
    [
        pytest.param("send the resolved report file", id="contract-text"),
        pytest.param("send the resolved report file\n", id="trailing-newline"),
        pytest.param("\t\n send the resolved report file \n\t", id="surrounding-whitespace"),
        pytest.param("send the draft report file", id="different-copy"),
    ],
)
@pytest.mark.asyncio
async def test_agent_work_executes_with_empty_conversation_context(
    monkeypatch: pytest.MonkeyPatch,
    trigger_content: str,
) -> None:
    environment_prompt = "scheduled environment"
    guidance_prompt = "scheduled guidance"
    attachments = ["reports/report.txt"]
    spec = AgentWorkSpec(
        instruction="send the resolved report file",
        execution_scope=END_SESSION_SCOPE_TASK,
        report_target=AgentWorkReportTarget.SESSION,
        instruction_message_id=8,
    )
    work = SimpleNamespace(
        id=12,
        sequence_no=3,
        uid="user-1",
        session_id="session-1",
        profile_id=4,
        source_id="21",
        dedupe_key="agent-work:scheduled:21:8",
        execution_state={
            "agent_work": spec.model_dump(mode="json"),
            "frozen_context_message_ids": [],
        },
    )
    trigger = Message(
        id=8,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        role=MessageRole.USER,
        type=MessageType.SCHEDULED_TASK_TRIGGER,
        content=trigger_content,
        environment_prompt=environment_prompt,
        guidance_prompt=guidance_prompt,
        attachments=attachments,
        is_processed=True,
    )
    captured = {}

    async def get_profile(_db, profile_id):
        assert profile_id == work.profile_id
        return SimpleNamespace(id=profile_id, uid=work.uid)

    async def get_message(_db, message_id):
        assert message_id == spec.instruction_message_id
        return trigger

    async def get_checkpoint(_db, work_id):
        assert work_id == work.id
        return None

    async def dispatch_interactive_work(_db, **kwargs):
        captured.update(kwargs)
        return {"content": "done", "history": [], "files": []}

    monkeypatch.setattr(executor_agent_work_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_agent_work_module.message_crud, "get", get_message)
    monkeypatch.setattr(executor_agent_work_module.session_reply_work_item_crud, "get_checkpoint", get_checkpoint)
    monkeypatch.setattr(executor_agent_work_module, "_dispatch_interactive_work", dispatch_interactive_work)

    response = await executor_agent_work_module.execute_agent_work(object(), work, "worker-1")

    assert response["content"] == "done"
    assert captured["message"] == spec.instruction
    assert captured["initial_message"].content == spec.instruction
    assert captured["initial_message"].id == trigger.id
    assert captured["initial_message"].environment_prompt == environment_prompt
    assert captured["initial_message"].guidance_prompt == guidance_prompt
    assert captured["initial_message"].execution_guidance == SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
    assert captured["initial_message"].attachments == attachments
    assert captured["execution_scope"] == END_SESSION_SCOPE_TASK
    assert captured["allow_additional_user_messages"] is False
    assert captured["frozen_context_messages"] == []
    assert captured["frozen_user_message_ids"] == [8]
    assert captured["attachments"] == attachments


def test_agent_work_parent_report_requires_task_scope() -> None:
    with pytest.raises(ValidationError):
        AgentWorkSpec(
            instruction="inspect the build",
            execution_scope=END_SESSION_SCOPE_FOREGROUND,
            report_target=AgentWorkReportTarget.PARENT_WORK,
            parent_work_id=17,
            subagent_id="a" * 32,
            parent_tool_call_id="call-parent",
        )


@pytest.mark.parametrize("model", [CreateArguments, SendArguments])
@pytest.mark.parametrize(
    "message, is_valid",
    [
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="spaces"),
        pytest.param("\t\n", False, id="tab-and-newline"),
        pytest.param("\n  inspect the build  \t\n", True, id="surrounding-whitespace"),
    ],
)
def test_agent_work_message_validation_preserves_valid_text(
    model: type[CreateArguments] | type[SendArguments],
    message: str,
    is_valid: bool,
) -> None:
    arguments = {"message": message}
    if model is SendArguments:
        arguments["subagent_id"] = "a" * 32

    if not is_valid:
        with pytest.raises(ValidationError) as exc_info:
            model(**arguments)
        assert any(error["loc"] == ("message",) for error in exc_info.value.errors())
        return

    parsed = model(**arguments)
    assert parsed.message == message
