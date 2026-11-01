from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.constants import END_SESSION_SCOPE_TASK
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.session_reply_queue import executor_agent_work as executor_agent_work_module
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message, MessageRole, MessageType


def test_agent_work_spec_requires_parent_for_parent_report_target() -> None:
    with pytest.raises(ValidationError):
        AgentWorkSpec(
            instruction="inspect the build",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.PARENT_WORK,
        )

    spec = AgentWorkSpec(
        instruction="inspect the build",
        execution_scope=END_SESSION_SCOPE_TASK,
        report_target=AgentWorkReportTarget.PARENT_WORK,
        parent_work_id=17,
    )

    assert spec.parent_work_id == 17


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
            "frozen_context_messages": [],
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

    async def dispatch_interactive_work(_db, **kwargs):
        captured.update(kwargs)
        return {"content": "done", "history": [], "files": []}

    monkeypatch.setattr(executor_agent_work_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_agent_work_module.message_crud, "get", get_message)
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


@pytest.mark.asyncio
async def test_agent_work_rejects_unimplemented_parent_report_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = AgentWorkSpec(
        instruction="inspect the build",
        execution_scope=END_SESSION_SCOPE_TASK,
        report_target=AgentWorkReportTarget.PARENT_WORK,
        parent_work_id=17,
    )
    work = SimpleNamespace(
        id=18,
        uid="user-1",
        session_id="child-session",
        profile_id=4,
        execution_state={
            "agent_work": spec.model_dump(mode="json"),
            "frozen_context_messages": [],
        },
    )

    async def get_profile(_db, profile_id):
        return SimpleNamespace(id=profile_id, uid=work.uid)

    monkeypatch.setattr(executor_agent_work_module.profile_crud, "get_with_relations", get_profile)

    with pytest.raises(RuntimeError):
        await executor_agent_work_module.execute_agent_work(object(), work, "worker-1")
