import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.constants import (
    END_SESSION_SCOPE_FOREGROUND,
    END_SESSION_SCOPE_TASK,
    ERR_SUBAGENT_ARGUMENTS_INVALID,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
)
from app.core.i18n import t
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.session_reply_queue import executor_agent_work as executor_agent_work_module
from app.core.tools.agent_work import (
    AgentWorkExecutor,
    BoardPostArguments,
    BoardReadArguments,
    CreateArguments,
    SendArguments,
)
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
    if model is CreateArguments:
        arguments["name"] = "构建检查"
    else:
        arguments["subagent_id"] = "a" * 32

    if not is_valid:
        with pytest.raises(ValidationError) as exc_info:
            model(**arguments)
        assert any(error["loc"] == ("message",) for error in exc_info.value.errors())
        return

    parsed = model(**arguments)
    assert parsed.message == message


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"message": "inspect the build"}, id="missing"),
        pytest.param({"message": "inspect the build", "name": ""}, id="empty"),
        pytest.param({"message": "inspect the build", "name": " \t "}, id="whitespace"),
        pytest.param({"message": "inspect the build", "name": None}, id="none"),
        pytest.param({"message": "inspect the build", "name": 7}, id="integer"),
        pytest.param({"message": "inspect the build", "name": "n" * 33}, id="too-long"),
        pytest.param({"message": "inspect the build", "name": "line\nbreak"}, id="newline"),
        pytest.param({"message": "inspect the build", "name": "tab\tcharacter"}, id="tab"),
        pytest.param({"message": "inspect the build", "name": "line\u0085break"}, id="next-line"),
        pytest.param({"message": "inspect the build", "name": "line\u2028break"}, id="line-separator"),
        pytest.param({"message": "inspect the build", "name": "line\u2029break"}, id="paragraph-separator"),
        pytest.param({"message": "inspect the build", "name": "control\u009bcharacter"}, id="c1-control"),
        pytest.param({"message": "inspect the build", "name": "nul\x00character"}, id="nul"),
        pytest.param({"message": "inspect the build", "name": "del\x7fcharacter"}, id="del"),
    ],
)
def test_create_arguments_reject_invalid_names(arguments: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateArguments(**arguments)

    assert any(error["loc"] == ("name",) for error in exc_info.value.errors())


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("x", id="one-character"),
        pytest.param("n" * 32, id="32-characters"),
        pytest.param("构建检查", id="chinese"),
        pytest.param(" 构建检查 ", id="preserve-surrounding-spaces"),
    ],
)
def test_create_arguments_preserve_valid_name(name: str) -> None:
    parsed = CreateArguments(message="inspect the build", name=name)

    assert parsed.name == name


def test_send_arguments_does_not_require_or_accept_name() -> None:
    parsed = SendArguments(message="continue previous task", subagent_id="a" * 32)

    assert parsed.message == "continue previous task"
    assert parsed.subagent_id == "a" * 32

    with pytest.raises(ValidationError) as exc_info:
        SendArguments(
            message="continue previous task",
            subagent_id="a" * 32,
            name="follow-up",
        )

    assert any(error["loc"] == ("name",) for error in exc_info.value.errors())


@pytest.mark.parametrize("missing_field", ["stage", "content"])
def test_board_post_arguments_require_stage_and_content(missing_field: str) -> None:
    arguments = {"stage": "running", "content": "progress update"}
    arguments.pop(missing_field)

    with pytest.raises(ValidationError) as exc_info:
        BoardPostArguments(**arguments)

    assert any(error["loc"] == (missing_field,) and error["type"] == "missing" for error in exc_info.value.errors())


@pytest.mark.parametrize("field", ["stage", "content"])
@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        pytest.param("", "string_too_short", id="empty"),
        pytest.param(" \t\n", "value_error", id="whitespace"),
        pytest.param(None, "string_type", id="none"),
        pytest.param(7, "string_type", id="integer"),
        pytest.param(True, "string_type", id="boolean"),
    ],
)
def test_board_post_arguments_reject_invalid_text_types(
    field: str,
    value: object,
    error_type: str,
) -> None:
    arguments = {"stage": "running", "content": "progress update"}
    arguments[field] = value

    with pytest.raises(ValidationError) as exc_info:
        BoardPostArguments(**arguments)

    assert any(error["loc"] == (field,) and error["type"] == error_type for error in exc_info.value.errors())


def test_board_post_arguments_enforce_stage_length() -> None:
    accepted = BoardPostArguments(stage="s" * 64, content="progress update")
    assert accepted.stage == "s" * 64

    with pytest.raises(ValidationError) as exc_info:
        BoardPostArguments(stage="s" * 65, content="progress update")

    assert any(error["loc"] == ("stage",) and error["type"] == "string_too_long" for error in exc_info.value.errors())


def test_board_post_arguments_preserve_formatted_content() -> None:
    content = "\n  progress update  \t\n"

    parsed = BoardPostArguments(stage="running", content=content)

    assert parsed.content == content


@pytest.mark.parametrize("extra_field", ["subagent_id", "work_id", "uid", "session_id", "profile_id", "message"])
def test_board_post_arguments_reject_extra_fields(extra_field: str) -> None:
    arguments = {"stage": "running", "content": "progress update", extra_field: "unexpected"}

    with pytest.raises(ValidationError) as exc_info:
        BoardPostArguments(**arguments)

    assert any(error["loc"] == (extra_field,) and error["type"] == "extra_forbidden" for error in exc_info.value.errors())


def test_board_read_arguments_require_subagent_id() -> None:
    with pytest.raises(ValidationError) as exc_info:
        BoardReadArguments()

    assert any(error["loc"] == ("subagent_id",) and error["type"] == "missing" for error in exc_info.value.errors())


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        pytest.param("a", "a", id="one-character"),
        pytest.param("a" * 32, "a" * 32, id="32-characters"),
        pytest.param("  agent-1 \t", "agent-1", id="trim-surrounding-whitespace"),
    ],
)
def test_board_read_arguments_trim_valid_subagent_id(raw_value: str, expected: str) -> None:
    parsed = BoardReadArguments(subagent_id=raw_value)

    assert parsed.subagent_id == expected


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        pytest.param("", "string_too_short", id="empty"),
        pytest.param(" \t\n", "string_too_short", id="blank-after-trim"),
        pytest.param(None, "string_type", id="none"),
        pytest.param(7, "string_type", id="integer"),
        pytest.param(True, "string_type", id="boolean"),
        pytest.param("a" * 33, "string_too_long", id="33-characters"),
    ],
)
def test_board_read_arguments_reject_invalid_subagent_id(value: object, error_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        BoardReadArguments(subagent_id=value)

    assert any(error["loc"] == ("subagent_id",) and error["type"] == error_type for error in exc_info.value.errors())


@pytest.mark.parametrize("extra_field", ["work_id", "stage", "content", "range"])
def test_board_read_arguments_reject_extra_fields(extra_field: str) -> None:
    arguments = {"subagent_id": "agent-1", extra_field: "unexpected"}

    with pytest.raises(ValidationError) as exc_info:
        BoardReadArguments(**arguments)

    assert any(error["loc"] == (extra_field,) and error["type"] == "extra_forbidden" for error in exc_info.value.errors())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["wait", "unknown"])
async def test_agent_work_executor_rejects_unsupported_operations_without_context(operation: str) -> None:
    payload = json.loads(await AgentWorkExecutor(project_root=".").execute(operation=operation))

    assert payload == {
        "status": "failed",
        "operation": operation,
        "error": t(ERR_SUBAGENT_ARGUMENTS_INVALID),
        "details": [{"field": "operation", "type": "invalid_operation"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        pytest.param(
            "board_post",
            {"stage": "running", "content": "progress update"},
            id="board-post",
        ),
        pytest.param("board_read", {"subagent_id": "agent-1"}, id="board-read"),
    ],
)
async def test_agent_work_executor_reports_missing_context_for_board_operations(
    operation: str,
    arguments: dict[str, object],
) -> None:
    payload = json.loads(await AgentWorkExecutor(project_root=".").execute(operation=operation, **arguments))

    assert payload == {
        "status": "failed",
        "operation": operation,
        "error": t(ERR_TOOL_RUNTIME_CONTEXT_MISSING),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments", "expected_details", "sensitive_value"),
    [
        pytest.param(
            "board_post",
            {"stage": "running", "content": {"secret": "private-board-content"}},
            [{"field": "content", "type": "string_type"}],
            "private-board-content",
            id="board-post-strict-type",
        ),
        pytest.param(
            "board_read",
            {"subagent_id": {"secret": "private-subagent-id"}},
            [{"field": "subagent_id", "type": "string_type"}],
            "private-subagent-id",
            id="board-read-strict-type",
        ),
        pytest.param(
            "board_post",
            {"stage": "running", "content": "progress update", "message": "private-extra"},
            [{"field": "message", "type": "extra_forbidden"}],
            "private-extra",
            id="board-post-extra-field",
        ),
    ],
)
async def test_agent_work_executor_returns_sanitized_argument_errors(
    operation: str,
    arguments: dict[str, object],
    expected_details: list[dict[str, str]],
    sensitive_value: str,
) -> None:
    payload = json.loads(await AgentWorkExecutor(project_root=".").execute(operation=operation, **arguments))

    assert payload["status"] == "failed"
    assert payload["operation"] == operation
    assert payload["error"] == t(ERR_SUBAGENT_ARGUMENTS_INVALID)
    assert payload["details"] == expected_details
    assert sensitive_value not in json.dumps(payload, ensure_ascii=False)
