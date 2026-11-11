import json
from pathlib import Path

import pytest
import pytest_asyncio

from app.core import constants as C
from app.core.i18n import t
from app.core.tools import get_tools_for_profile
from app.core.tools.agent_work import AGENT_WORK_TOOL_SCHEMA
from app.core.tools.file_tool import FILE_TOOL_SCHEMA
from app.core.tools.shell import SHELL_TOOL_SCHEMA
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round, process_single_tool
from app.models.message import InternalToolCall
from app.models.profile import Profile, ProfileConfig

ENABLED_TOOLS = (
    C.SHELL_TOOL_NAME,
    C.FILE_TOOL_NAME,
    C.WEB_TOOL_NAME,
    C.SEND_FILE_TO_USER_TOOL_NAME,
    C.READ_MULTIMODAL_FILE_TOOL_NAME,
    C.MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    C.MANAGE_BACKGROUND_TASKS_TOOL_NAME,
)
BUILTINS = {C.MANAGE_TODO_TOOL_NAME, C.END_SESSION_TOOL_NAME, C.SKILL_TOOL_NAME}
SHELL_TOOLS = {C.SHELL_TOOL_NAME, C.TERMINAL_TOOL_NAME, C.TERMINAL_WATCH_TOOL_NAME}
CONFIGURED = set(ENABLED_TOOLS) | {C.TERMINAL_TOOL_NAME, C.TERMINAL_WATCH_TOOL_NAME}
WORKFLOW_CASES = [
    pytest.param(C.TOOL_WORKFLOW_FOREGROUND, BUILTINS | CONFIGURED, set(), id="foreground"),
    pytest.param(C.TOOL_WORKFLOW_SCHEDULED_TASK, (BUILTINS - {C.MANAGE_TODO_TOOL_NAME}) | (CONFIGURED - {C.MANAGE_SCHEDULED_TASKS_TOOL_NAME}), {C.MANAGE_TODO_TOOL_NAME, C.MANAGE_SCHEDULED_TASKS_TOOL_NAME}, id="scheduled"),
    pytest.param(
        C.TOOL_WORKFLOW_SUBAGENT,
        {C.END_SESSION_TOOL_NAME, C.SKILL_TOOL_NAME, C.FILE_TOOL_NAME, C.WEB_TOOL_NAME, C.READ_MULTIMODAL_FILE_TOOL_NAME} | SHELL_TOOLS,
        {C.MANAGE_TODO_TOOL_NAME, C.MANAGE_SCHEDULED_TASKS_TOOL_NAME, C.MANAGE_BACKGROUND_TASKS_TOOL_NAME, C.SEND_FILE_TO_USER_TOOL_NAME, C.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME},
        id="subagent",
    ),
    pytest.param(C.TOOL_WORKFLOW_BACKGROUND_PROACTIVE, {C.SEND_FILE_TO_USER_TOOL_NAME}, BUILTINS | (CONFIGURED - {C.SEND_FILE_TO_USER_TOOL_NAME}) | {C.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME}, id="background-without-kb"),
]


@pytest_asyncio.fixture
async def workflow_db(database_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    async with database_factory(name="workflow-tool-policy.sqlite3") as factory, factory() as db:
        yield db


def _profile(tmp_path: Path, *, memory_enabled: bool = False, enabled_tools: list[str] | None = None) -> tuple[Profile, ProfileConfig]:
    cfg = ProfileConfig.model_validate(
        {
            "tool": {
                "enabled_tools": list(ENABLED_TOOLS if enabled_tools is None else enabled_tools),
                "allowed_operation_dirs": [str(tmp_path.resolve())],
            },
            "memory": {"enabled": memory_enabled, "precheck_enabled": False},
        }
    )
    return Profile(id=1, uid="workflow-owner", name="workflow policy profile", configs=cfg.model_dump(mode="json")), cfg


def _names(tools: list[dict]) -> set[str]:
    return {tool["function"]["name"] for tool in tools}


def _schema(tools: list[dict], name: str) -> dict:
    return next(tool for tool in tools if tool["function"]["name"] == name)


def _call(call_id: str, name: str, arguments: dict) -> InternalToolCall:
    return InternalToolCall(id=call_id, name=name, arguments=arguments)


def _files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


async def _run(db, profile: Profile, cfg: ProfileConfig, call: InternalToolCall, schemas: list[dict], workflow: str):
    return await process_single_tool(call, db, profile, cfg, [], profile.uid, "workflow-session", 1, profile.uid, tool_schemas=schemas, tool_workflow=workflow)


async def _assert_rejected(db, profile, cfg, call, schemas, workflow, target: Path, before_files: set[Path], *, check_error: bool = True) -> None:
    before_content = target.read_text(encoding="utf-8")
    expected_error = t(C.ERR_TOOL_NOT_ENABLED, tool_name=call.name)
    errors = prevalidate_tool_round([call], cfg, tool_schemas=schemas, tool_workflow=workflow)
    precheck = json.loads(errors[call.id])
    assert precheck["status"] == "failed"
    if check_error:
        assert precheck["error"] == expected_error
    result = await _run(db, profile, cfg, call, schemas, workflow)
    payload = json.loads(result.content)
    assert result.tool_execution_status == "failed"
    assert payload["status"] == "failed"
    if check_error:
        assert payload["error"] == expected_error
    assert target.read_text(encoding="utf-8") == before_content
    assert _files(target.parent) == before_files


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_enabled", [False, True], ids=["memory-off", "memory-on"])
@pytest.mark.parametrize(("workflow", "expected", "forbidden"), WORKFLOW_CASES)
async def test_workflow_tool_exposure(workflow_db, tmp_path: Path, workflow: str, expected: set[str], forbidden: set[str], memory_enabled: bool) -> None:
    profile, _ = _profile(tmp_path, memory_enabled=memory_enabled)
    tools, whitelist = await get_tools_for_profile(workflow_db, profile, tool_workflow=workflow)
    expected_names = set(expected)
    if memory_enabled and workflow in {C.TOOL_WORKFLOW_FOREGROUND, C.TOOL_WORKFLOW_SCHEDULED_TASK}:
        expected_names.add(C.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME)
    assert _names(tools) == expected_names
    assert _names(tools).isdisjoint(forbidden)
    assert whitelist == []


@pytest.mark.asyncio
async def test_subagent_file_operations_use_filtered_schema_and_real_executor(workflow_db, tmp_path: Path) -> None:
    profile, cfg = _profile(tmp_path)
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    file_schema = _schema(tools, C.FILE_TOOL_NAME)
    assert file_schema["function"]["parameters"]["properties"]["operation"]["enum"] == ["read", "write", "edit", "patch", "grep"]

    target = tmp_path / "subagent.txt"
    steps = [
        ("write", {"content": "alpha\nbeta\n"}, "alpha\nbeta\n"),
        ("edit", {"old_text": "alpha", "new_text": "delta"}, "delta\nbeta\n"),
        ("patch", {"patch": "@@\n-beta\n+gamma\n"}, "delta\ngamma\n"),
        ("read", {}, "delta\ngamma\n"),
        ("grep", {"pattern": "gamma", "before_chars": 2, "after_chars": 1}, "delta\ngamma\n"),
    ]
    for index, (operation, arguments, expected_text) in enumerate(steps):
        call = _call(f"subagent-file-{index}", C.FILE_TOOL_NAME, {"operation": operation, "path": str(target), **arguments})
        result = await _run(workflow_db, profile, cfg, call, tools, C.TOOL_WORKFLOW_SUBAGENT)
        assert result.tool_execution_status == "succeeded"
        payload = json.loads(result.content)
        assert payload["status"] == "success"
        assert payload["operation"] == operation
        assert target.read_text(encoding="utf-8") == expected_text
        if operation == "read":
            assert payload["content"] == "1: delta\n2: gamma"
        elif operation == "grep":
            assert payload["match_count"] == 1
            assert payload["matches"][0]["content"] == "a\ngamma\n"
            assert payload["matches"][0]["content_omitted"] is False


@pytest.mark.asyncio
async def test_subagent_grep_retries_omitted_content_with_character_context(workflow_db, tmp_path: Path) -> None:
    profile, cfg = _profile(tmp_path)
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    file_schema = _schema(tools, C.FILE_TOOL_NAME)
    schemas = [file_schema]

    original_content = "x" * (700 * 1024) + "needle tail"
    target = tmp_path / "long-line.txt"
    target.write_text(original_content, encoding="utf-8")

    first_call = _call(
        "subagent-grep-omitted",
        C.FILE_TOOL_NAME,
        {"operation": "grep", "path": str(target), "pattern": "needle"},
    )
    assert prevalidate_tool_round([first_call], cfg, tool_schemas=schemas, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT) == {}
    first_result = await _run(workflow_db, profile, cfg, first_call, schemas, C.TOOL_WORKFLOW_SUBAGENT)
    assert first_result.tool_execution_status == "succeeded"
    first_payload = json.loads(first_result.content)
    assert first_payload["status"] == "success"
    assert first_payload["match_count"] == 1
    assert len(first_payload["matches"]) == 1
    first_match = first_payload["matches"][0]
    assert first_match["path"] == str(target)
    first_coordinates = (
        first_match["start_line"],
        first_match["end_line"],
        first_match["start_column"],
        first_match["end_column"],
    )
    assert first_coordinates == (1, 1, 700 * 1024 + 1, 700 * 1024 + 6)
    assert first_match["content_omitted"] is True
    assert "before_chars" in first_payload["instruction"]
    assert "after_chars" in first_payload["instruction"]
    assert len(first_result.content.encode("utf-8")) <= 65536
    assert target.read_text(encoding="utf-8") == original_content

    retry_call = _call(
        "subagent-grep-retry",
        C.FILE_TOOL_NAME,
        {
            "operation": "grep",
            "path": str(target),
            "pattern": "needle",
            "before_chars": 2,
            "after_chars": 5,
        },
    )
    assert prevalidate_tool_round([retry_call], cfg, tool_schemas=schemas, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT) == {}
    retry_result = await _run(workflow_db, profile, cfg, retry_call, schemas, C.TOOL_WORKFLOW_SUBAGENT)
    assert retry_result.tool_execution_status == "succeeded"
    retry_payload = json.loads(retry_result.content)
    assert retry_payload["status"] == "success"
    assert retry_payload["match_count"] == 1
    assert len(retry_payload["matches"]) == 1
    retry_match = retry_payload["matches"][0]
    retry_coordinates = (
        retry_match["start_line"],
        retry_match["end_line"],
        retry_match["start_column"],
        retry_match["end_column"],
    )
    assert retry_coordinates == first_coordinates
    assert retry_match["content"] == "xxneedle tail"
    assert retry_match["content_omitted"] is False
    assert "instruction" not in retry_payload
    assert target.read_text(encoding="utf-8") == original_content


@pytest.mark.asyncio
async def test_file_tool_rejects_run_in_background_from_complete_schema(workflow_db, tmp_path: Path) -> None:
    profile, cfg = _profile(tmp_path)
    target = tmp_path / "foreground.txt"
    before_content = "stable\n"
    target.write_text(before_content, encoding="utf-8")
    call = _call(
        "foreground-file-background-flag",
        C.FILE_TOOL_NAME,
        {"operation": "write", "path": str(target), "content": "must not write", "run_in_background": True},
    )

    errors = prevalidate_tool_round([call], cfg, tool_schemas=[FILE_TOOL_SCHEMA], tool_workflow=C.TOOL_WORKFLOW_FOREGROUND)
    precheck = json.loads(errors[call.id])
    assert precheck["status"] == "failed"
    assert precheck["unsupported_arguments"] == ["run_in_background"]

    result = await _run(workflow_db, profile, cfg, call, [FILE_TOOL_SCHEMA], C.TOOL_WORKFLOW_FOREGROUND)
    assert result.tool_execution_status == "failed"
    payload = json.loads(result.content)
    assert payload["status"] == "failed"
    assert payload["unsupported_arguments"] == ["run_in_background"]
    assert target.read_text(encoding="utf-8") == before_content


@pytest.mark.asyncio
async def test_subagent_shell_companions_follow_profile_policy_and_prevalidate(workflow_db, tmp_path: Path) -> None:
    profile, cfg = _profile(tmp_path, enabled_tools=[*ENABLED_TOOLS, C.AGENT_WORK_TOOL_NAME])
    source_operation_enum = list(AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"])
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    names = _names(tools)

    assert SHELL_TOOLS <= names
    assert _schema(tools, C.AGENT_WORK_TOOL_NAME)["function"]["parameters"]["properties"]["operation"]["enum"] == ["board_post"]
    assert set(_schema(tools, C.SHELL_TOOL_NAME)["function"]["parameters"]["properties"]["execution_mode"]["enum"]) == {"non_interactive", "interactive"}
    assert set(_schema(tools, C.TERMINAL_TOOL_NAME)["function"]["parameters"]["properties"]["operation"]["enum"]) == set(C.TERMINAL_TOOL_OPERATIONS)
    assert AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"] == source_operation_enum

    for execution_mode in ("non_interactive", "interactive"):
        call = _call(f"subagent-shell-{execution_mode}", C.SHELL_TOOL_NAME, {"command": "echo prevalidation-only", "execution_mode": execution_mode})
        assert prevalidate_tool_round([call], cfg, tool_schemas=tools, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT) == {}

    board_post = _call(
        "subagent-agent-work-board-post",
        C.AGENT_WORK_TOOL_NAME,
        {"operation": "board_post", "stage": "verification", "content": "role policy checked"},
    )
    assert prevalidate_tool_round([board_post], cfg, tool_schemas=tools, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT) == {}

    disabled_profile, _ = _profile(tmp_path, enabled_tools=[name for name in ENABLED_TOOLS if name != C.SHELL_TOOL_NAME])
    disabled_tools, _ = await get_tools_for_profile(workflow_db, disabled_profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    assert _names(disabled_tools).isdisjoint(SHELL_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "expected_operations"),
    [
        pytest.param(
            C.TOOL_WORKFLOW_FOREGROUND,
            {"create", "list", "query_result", "send_message", "cancel", "board_read"},
            id="foreground",
        ),
        pytest.param(C.TOOL_WORKFLOW_SCHEDULED_TASK, set(), id="scheduled"),
        pytest.param(C.TOOL_WORKFLOW_BACKGROUND_PROACTIVE, set(), id="background"),
        pytest.param(C.TOOL_WORKFLOW_AUDIT, set(), id="audit"),
    ],
)
async def test_agent_work_workflow_exposure(workflow_db, tmp_path: Path, workflow: str, expected_operations: set[str]) -> None:
    profile, _ = _profile(tmp_path, enabled_tools=[*ENABLED_TOOLS, C.AGENT_WORK_TOOL_NAME])
    source_operation_enum = list(AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"])
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=workflow)

    if expected_operations:
        operation_enum = _schema(tools, C.AGENT_WORK_TOOL_NAME)["function"]["parameters"]["properties"]["operation"]["enum"]
        assert set(operation_enum) == expected_operations
        assert {"wait", "board_post"}.isdisjoint(operation_enum)
    else:
        assert C.AGENT_WORK_TOOL_NAME not in _names(tools)
    assert AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"] == source_operation_enum


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        pytest.param("create", {"operation": "create", "name": "child-task", "message": "inspect independently"}, id="create"),
        pytest.param("list", {"operation": "list"}, id="list"),
        pytest.param("query_result", {"operation": "query_result", "subagent_id": "child-1", "work_id": 1}, id="query-result"),
        pytest.param("send_message", {"operation": "send_message", "subagent_id": "child-1", "message": "continue"}, id="send-message"),
        pytest.param("cancel", {"operation": "cancel", "subagent_id": "child-1", "work_id": 1}, id="cancel"),
        pytest.param("board_read", {"operation": "board_read", "subagent_id": "child-1"}, id="board-read"),
    ],
)
async def test_main_agent_work_operations_pass_prevalidation(workflow_db, tmp_path: Path, operation: str, arguments: dict[str, object]) -> None:
    profile, cfg = _profile(tmp_path, enabled_tools=[*ENABLED_TOOLS, C.AGENT_WORK_TOOL_NAME])
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_FOREGROUND)
    call = _call(f"main-agent-work-{operation}", C.AGENT_WORK_TOOL_NAME, arguments)
    assert prevalidate_tool_round([call], cfg, tool_schemas=tools, tool_workflow=C.TOOL_WORKFLOW_FOREGROUND) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        pytest.param("create", {"operation": "create", "name": "nested-task", "message": "must be rejected"}, id="create"),
        pytest.param("list", {"operation": "list"}, id="list"),
        pytest.param("query_result", {"operation": "query_result", "subagent_id": "parent-child", "work_id": 1}, id="query-result"),
        pytest.param("send_message", {"operation": "send_message", "subagent_id": "parent-child", "message": "must be rejected"}, id="send-message"),
        pytest.param("cancel", {"operation": "cancel", "subagent_id": "parent-child", "work_id": 1}, id="cancel"),
        pytest.param("board_read", {"operation": "board_read", "subagent_id": "parent-child"}, id="board-read"),
        pytest.param("wait", {"operation": "wait", "subagent_id": "parent-child", "work_id": 1}, id="wait"),
    ],
)
async def test_subagent_agent_work_rejects_primary_operations(workflow_db, tmp_path: Path, operation: str, arguments: dict[str, object]) -> None:
    profile, cfg = _profile(tmp_path, enabled_tools=[*ENABLED_TOOLS, C.AGENT_WORK_TOOL_NAME])
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    call = _call(f"subagent-agent-work-{operation}", C.AGENT_WORK_TOOL_NAME, arguments)
    errors = prevalidate_tool_round([call], cfg, tool_schemas=tools, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    assert call.id in errors
    assert json.loads(errors[call.id])["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "arguments"),
    [
        pytest.param(C.TOOL_WORKFLOW_FOREGROUND, {"operation": "list"}, id="foreground"),
        pytest.param(
            C.TOOL_WORKFLOW_SUBAGENT,
            {"operation": "board_post", "stage": "verification", "content": "must be rejected"},
            id="subagent",
        ),
    ],
)
async def test_agent_work_disabled_profile_is_hidden_and_rejected(workflow_db, tmp_path: Path, workflow: str, arguments: dict[str, object]) -> None:
    profile, cfg = _profile(tmp_path, enabled_tools=list(ENABLED_TOOLS))
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=workflow)
    assert C.AGENT_WORK_TOOL_NAME not in _names(tools)

    target = tmp_path / "agent-work-disabled.txt"
    target.write_text("stable\n", encoding="utf-8")
    before_files = _files(tmp_path)
    call = _call(f"disabled-agent-work-{workflow}", C.AGENT_WORK_TOOL_NAME, arguments)
    await _assert_rejected(workflow_db, profile, cfg, call, [AGENT_WORK_TOOL_SCHEMA], workflow, target, before_files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "tool_name", "profile_tool_enabled", "empty_schemas"),
    [
        pytest.param(C.TOOL_WORKFLOW_SUBAGENT, C.SHELL_TOOL_NAME, False, False, id="profile-shell-disabled"),
        pytest.param(C.TOOL_WORKFLOW_BACKGROUND_PROACTIVE, C.SHELL_TOOL_NAME, True, False, id="background-shell"),
        pytest.param(C.TOOL_WORKFLOW_BACKGROUND_PROACTIVE, C.FILE_TOOL_NAME, True, False, id="background-file"),
        pytest.param(C.TOOL_WORKFLOW_SUBAGENT, C.FILE_TOOL_NAME, False, False, id="profile-file-disabled"),
        pytest.param(C.TOOL_WORKFLOW_FOREGROUND, C.FILE_TOOL_NAME, True, True, id="empty-tool-schemas"),
    ],
)
async def test_forbidden_workflow_or_profile_blocks_before_execution(workflow_db, tmp_path: Path, workflow: str, tool_name: str, profile_tool_enabled: bool, empty_schemas: bool) -> None:
    enabled = [name for name in ENABLED_TOOLS if profile_tool_enabled or name != tool_name]
    profile, cfg = _profile(tmp_path, enabled_tools=enabled)
    target = tmp_path / "protected.txt"
    target.write_text("stable\n", encoding="utf-8")
    before_files = _files(tmp_path)
    if tool_name == C.SHELL_TOOL_NAME:
        arguments = {"command": "echo workflow-policy-marker", "execution_mode": "non_interactive"}
        schemas = [SHELL_TOOL_SCHEMA]
    else:
        arguments = {"operation": "write", "path": str(target), "content": "must not write"}
        schemas = [] if empty_schemas else [FILE_TOOL_SCHEMA]
    await _assert_rejected(workflow_db, profile, cfg, _call(f"blocked-{tool_name}-{workflow}", tool_name, arguments), schemas, workflow, target, before_files)


@pytest.mark.asyncio
async def test_subagent_policy_can_restrict_file_operations_without_mutating_source_schema(workflow_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile, cfg = _profile(tmp_path)
    source_enum = list(FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"])
    monkeypatch.setitem(C.TOOL_WORKFLOW_POLICIES[C.TOOL_WORKFLOW_SUBAGENT][C.FILE_TOOL_NAME], "operation", ("read", "grep"))
    tools, _ = await get_tools_for_profile(workflow_db, profile, tool_workflow=C.TOOL_WORKFLOW_SUBAGENT)
    assert _schema(tools, C.FILE_TOOL_NAME)["function"]["parameters"]["properties"]["operation"]["enum"] == ["read", "grep"]
    assert FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"] == source_enum

    target = tmp_path / "policy.txt"
    target.write_text("beta\n", encoding="utf-8")
    before_files = _files(tmp_path)
    call = _call("subagent-policy-write", C.FILE_TOOL_NAME, {"operation": "write", "path": str(target), "content": "gamma\n"})
    await _assert_rejected(workflow_db, profile, cfg, call, [FILE_TOOL_SCHEMA], C.TOOL_WORKFLOW_SUBAGENT, target, before_files, check_error=False)
    assert FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]["operation"]["enum"] == source_enum


@pytest.mark.asyncio
async def test_unknown_workflow_exposes_no_tools_and_rejects_execution(workflow_db, tmp_path: Path) -> None:
    profile, cfg = _profile(tmp_path)
    workflow = "unknown-workflow"
    tools, whitelist = await get_tools_for_profile(workflow_db, profile, tool_workflow=workflow)
    assert tools == []
    assert whitelist == []
    target = tmp_path / "unknown.txt"
    target.write_text("stable\n", encoding="utf-8")
    before_files = _files(tmp_path)
    call = _call("unknown-workflow-file", C.FILE_TOOL_NAME, {"operation": "write", "path": str(target), "content": "must not write"})
    await _assert_rejected(workflow_db, profile, cfg, call, [FILE_TOOL_SCHEMA], workflow, target, before_files)
