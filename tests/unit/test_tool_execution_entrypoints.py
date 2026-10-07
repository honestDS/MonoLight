import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.background_tasks.manager import background_task_manager
from app.core.constants import ERR_INTERNAL_SERVER_ERROR, ERR_TOOL_EXECUTION_TIMEOUT
from app.core.i18n import set_current_locale, t
from app.core.i18n.context import reset_current_locale
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.tools import TOOL_EXECUTOR_MAP, tool_requires_audit
from app.core.tools.base import BaseExecutor
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile, ProfileConfig


def test_registered_tools_explicitly_declare_audit_requirement():
    assert all(isinstance(executor_class.requires_audit, bool) for executor_class in TOOL_EXECUTOR_MAP.values())
    audited_tools = {"execute_shell", "file_tool", "terminal_write", "terminal_close"}
    assert {tool_name for tool_name in TOOL_EXECUTOR_MAP if tool_requires_audit(tool_name)} == audited_tools
    assert "read_multimodal_file" in TOOL_EXECUTOR_MAP
    assert tool_requires_audit("read_multimodal_file") is False
    assert all(not tool_requires_audit(tool_name) for tool_name in TOOL_EXECUTOR_MAP if tool_name not in audited_tools)
    assert tool_requires_audit("unknown_tool") is False


def test_confirmed_tool_result_budget_reuses_last_model_context_window():
    session = SimpleNamespace(llm_request_metadata={"context_window_tokens": 1_050_000})

    assert executor_confirmed_module._resolve_confirmed_tool_context_window_k(session) == 1050
    assert executor_confirmed_module._resolve_confirmed_tool_context_window_k(SimpleNamespace(llm_request_metadata=None)) == 4


def test_confirmed_tool_result_budget_uses_provider_input_plus_confirmed_tool_call(monkeypatch):
    session = SimpleNamespace(
        llm_request_metadata={
            "context_window_tokens": 250_000,
            "max_output_tokens": 20_480,
            "input_tokens": 185_765,
            "input_tokens_source": "provider",
        }
    )
    confirmed_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="call-confirmed",
                name="execute_shell",
                arguments={"command": "git diff", "execution_mode": "non_interactive"},
            )
        ],
    )
    monkeypatch.setattr(executor_confirmed_module, "estimate_tokens", lambda _text, **_kwargs: 5_000)
    monkeypatch.setattr(executor_confirmed_module, "message_token_text", lambda _message: "confirmed-tool-call")

    budget_tokens = executor_confirmed_module._resolve_confirmed_tool_result_round_budget_tokens(
        session,
        confirmed_message,
        tools=[],
    )

    assert budget_tokens == (250_000 - 20_480 - 256 - 190_765) // 2


def test_confirmed_tool_result_budget_keeps_minimal_budget_when_provider_baseline_already_exceeds_window(monkeypatch):
    session = SimpleNamespace(
        llm_request_metadata={
            "context_window_tokens": 250_000,
            "max_output_tokens": 20_480,
            "input_tokens": 352_375,
            "input_tokens_source": "provider",
        }
    )
    confirmed_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="call-confirmed",
                name="execute_shell",
                arguments={"command": "git diff", "execution_mode": "non_interactive"},
            )
        ],
    )
    monkeypatch.setattr(executor_confirmed_module, "estimate_tokens", lambda _text, **_kwargs: 2_000)
    monkeypatch.setattr(executor_confirmed_module, "message_token_text", lambda _message: "confirmed-tool-call")

    budget_tokens = executor_confirmed_module._resolve_confirmed_tool_result_round_budget_tokens(
        session,
        confirmed_message,
        tools=[],
    )

    assert budget_tokens == 1


@pytest.fixture
def _isolated_tool_dispatch(monkeypatch):
    monkeypatch.setattr(
        process_single_tool_module,
        "truncate_tool_messages_for_budget",
        lambda **_: SimpleNamespace(truncated_count=0),
    )
    monkeypatch.setattr(process_single_tool_module.LogManager, "log_tool_call", lambda *_, **__: None)
    monkeypatch.setattr(process_single_tool_module.LogManager, "log_tool_result", lambda *_, **__: None)


def _install_fake_executor(monkeypatch, tool_name: str, execute_callback):
    started = asyncio.Event()
    finished = asyncio.Event()

    class FakeExecutor(BaseExecutor):
        requires_audit = False

        async def execute(self, **kwargs) -> str:
            started.set()
            try:
                return await execute_callback()
            finally:
                finished.set()

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, tool_name, FakeExecutor)
    return started, finished


async def _call_tool(tool_name: str, arguments: dict, timeout: float, active_tasks: set[asyncio.Task]):
    cfg = ProfileConfig.model_validate(
        {
            "tool": {
                "tool_timeout": timeout,
                "enabled_tools": ["execute_shell", tool_name],
            },
        }
    )
    return await process_single_tool_module.process_single_tool(
        InternalToolCall(id=f"call-{tool_name}", name=tool_name, arguments=arguments),
        SimpleNamespace(info={}),
        Profile(uid="test-user", name="test-profile", configs=cfg.model_dump()),
        cfg,
        [],
        "test-user",
        "test-session",
        1,
        "test-user",
        active_tasks=active_tasks,
        tool_result_round_budget_tokens=1024,
    )


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [
        ("legacy result", "succeeded"),
        (json.dumps(["legacy", "result"]), "succeeded"),
        (json.dumps({}), "succeeded"),
        (json.dumps({"status": "success"}), "succeeded"),
        (json.dumps({"status": "failed"}), "failed"),
        (json.dumps({"error": "failure"}), "failed"),
        (json.dumps({"exit_code": 7}), "failed"),
        (json.dumps({"status": "execution_unknown", "error": "timeout"}), "execution_unknown"),
    ],
)
def test_legacy_tool_message_status_falls_back_to_content(content, expected_status):
    legacy_message = InternalMessage.model_validate_json(json.dumps({"role": "tool", "tool_call_id": "legacy-call", "content": content}))

    assert legacy_message.tool_execution_status is None
    status = process_single_tool_module.get_tool_execution_status(legacy_message)
    round_tripped_message = InternalMessage.model_validate_json(legacy_message.model_dump_json(exclude_none=True))

    assert status == expected_status
    assert process_single_tool_module.get_tool_execution_status(round_tripped_message) == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("firecrawl_scrape", {"url": "https://example.com"}),
        ("firecrawl_search", {"query": "timeout regression"}),
        ("terminal_status", {"terminal_session_id": "t" * 32}),
        ("future_test_tool", {}),
    ],
)
@pytest.mark.parametrize(
    ("locale", "message_fragment"),
    [
        ("zh", "底层操作可能仍在执行"),
        ("en", "underlying operation may still be running"),
    ],
)
async def test_regular_public_tools_use_configured_timeout_and_clean_up(
    monkeypatch,
    _isolated_tool_dispatch,
    tool_name,
    arguments,
    locale,
    message_fragment,
):
    timeout = 0.03
    wait_event = asyncio.Event()

    async def execute_callback():
        await wait_event.wait()

    _, finished = _install_fake_executor(monkeypatch, tool_name, execute_callback)
    active_tasks = set()
    locale_token = set_current_locale(locale)
    try:
        message = await asyncio.wait_for(_call_tool(tool_name, arguments, timeout, active_tasks), timeout=2)
        payload = json.loads(message.content or "{}")
        assert isinstance(message, InternalMessage)
        assert message.role == MessageRole.TOOL
        assert message.tool_call_id == f"call-{tool_name}"
        assert message.tool_execution_status == "execution_unknown"
        assert payload == {
            "status": "execution_unknown",
            "tool_name": tool_name,
            "error_code": ERR_TOOL_EXECUTION_TIMEOUT,
            "error": t(ERR_TOOL_EXECUTION_TIMEOUT, tool_name=tool_name, timeout=timeout),
        }
        assert message_fragment in payload["error"]
    finally:
        reset_current_locale(locale_token)
    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "expected_timeout"),
    [
        (0.02, True),
        (0.3, False),
    ],
)
async def test_regular_tool_timeout_uses_profile_value(monkeypatch, _isolated_tool_dispatch, timeout, expected_timeout):
    async def execute_callback():
        await asyncio.sleep(0.06)
        return "configured_timeout_result"

    _, finished = _install_fake_executor(monkeypatch, "future_test_tool", execute_callback)
    active_tasks = set()
    locale_token = set_current_locale("zh")
    try:
        message = await asyncio.wait_for(_call_tool("future_test_tool", {}, timeout, active_tasks), timeout=2)
    finally:
        reset_current_locale(locale_token)

    assert isinstance(message, InternalMessage)
    assert message.role == MessageRole.TOOL
    assert message.tool_call_id == "call-future_test_tool"
    if expected_timeout:
        assert message.tool_execution_status == "execution_unknown"
        payload = json.loads(message.content or "{}")
        assert payload["status"] == "execution_unknown"
        assert payload["error_code"] == ERR_TOOL_EXECUTION_TIMEOUT
        assert payload["error"] == t(
            ERR_TOOL_EXECUTION_TIMEOUT,
            locale="zh",
            tool_name="future_test_tool",
            timeout=timeout,
        )
    else:
        assert message.content == "configured_timeout_result"
        assert message.tool_execution_status == "succeeded"
    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("execute_shell", {"command": "echo test", "execution_mode": "interactive"}),
        ("execute_shell", {"command": "echo test", "execution_mode": "non_interactive"}),
        ("terminal_read", {"terminal_session_id": "t" * 32}),
        ("terminal_write", {"terminal_session_id": "t" * 32, "data": "input"}),
        ("terminal_resize", {"terminal_session_id": "t" * 32, "columns": 80, "rows": 24}),
        ("terminal_close", {"terminal_session_id": "t" * 32, "force": False}),
    ],
)
async def test_self_managed_tool_calls_are_not_wrapped_in_public_timeout(
    monkeypatch,
    _isolated_tool_dispatch,
    tool_name,
    arguments,
):
    async def execute_callback():
        await asyncio.sleep(0.06)
        return f"{tool_name}_timed_out"

    _, finished = _install_fake_executor(monkeypatch, tool_name, execute_callback)
    active_tasks = set()

    message = await asyncio.wait_for(_call_tool(tool_name, arguments, 0.02, active_tasks), timeout=2)

    assert isinstance(message, InternalMessage)
    assert message.role == MessageRole.TOOL
    assert message.tool_call_id == f"call-{tool_name}"
    assert message.content == f"{tool_name}_timed_out"
    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("future_test_tool", {}),
        ("execute_shell", {"command": "echo test", "execution_mode": "non_interactive"}),
    ],
)
async def test_tool_cancellation_propagates_and_cleans_up(monkeypatch, _isolated_tool_dispatch, tool_name, arguments):
    wait_event = asyncio.Event()

    async def execute_callback():
        await wait_event.wait()

    started, finished = _install_fake_executor(monkeypatch, tool_name, execute_callback)
    active_tasks = set()
    call_task = asyncio.create_task(_call_tool(tool_name, arguments, 1.0, active_tasks))

    await asyncio.wait_for(started.wait(), timeout=1)
    call_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call_task, timeout=2)

    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_error", [TimeoutError("executor timeout"), RuntimeError("executor failure")])
async def test_executor_errors_keep_internal_error_semantics(monkeypatch, _isolated_tool_dispatch, executor_error):
    async def execute_callback():
        raise executor_error

    _, finished = _install_fake_executor(monkeypatch, "future_test_tool", execute_callback)
    active_tasks = set()
    locale_token = set_current_locale("zh")
    try:
        message = await asyncio.wait_for(_call_tool("future_test_tool", {}, 0.3, active_tasks), timeout=2)
    finally:
        reset_current_locale(locale_token)

    payload = json.loads(message.content or "{}")

    assert message.tool_execution_status == "failed"
    assert payload == {
        "status": "failed",
        "tool_name": "future_test_tool",
        "error": t(ERR_INTERNAL_SERVER_ERROR, locale="zh"),
    }
    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
async def test_expired_regular_tool_result_stays_unknown_when_executor_swallows_cancel(
    monkeypatch,
    _isolated_tool_dispatch,
):
    wait_event = asyncio.Event()

    async def execute_callback():
        try:
            await wait_event.wait()
        except asyncio.CancelledError:
            return "success_after_cancel"

    _, finished = _install_fake_executor(monkeypatch, "future_test_tool", execute_callback)
    timeout = 0.02
    active_tasks = set()
    locale_token = set_current_locale("zh")
    try:
        message = await asyncio.wait_for(_call_tool("future_test_tool", {}, timeout, active_tasks), timeout=2)
    finally:
        reset_current_locale(locale_token)

    payload = json.loads(message.content or "{}")

    assert message.tool_execution_status == "execution_unknown"
    assert payload == {
        "status": "execution_unknown",
        "tool_name": "future_test_tool",
        "error_code": ERR_TOOL_EXECUTION_TIMEOUT,
        "error": t(ERR_TOOL_EXECUTION_TIMEOUT, locale="zh", tool_name="future_test_tool", timeout=timeout),
    }
    assert finished.is_set()
    assert active_tasks == set()


@pytest.mark.asyncio
async def test_always_background_image_submission_is_not_limited_by_foreground_execution_timeout(
    monkeypatch,
    _isolated_tool_dispatch,
):
    """验证后台提交行为，不验证前台工具排除集合。"""
    submissions = []

    async def fake_submit(db, **kwargs):
        submissions.append((db, kwargs))
        await asyncio.sleep(0.06)
        return SimpleNamespace(id=123)

    monkeypatch.setattr(background_task_manager, "submit", fake_submit)
    active_tasks = set()

    message = await asyncio.wait_for(
        _call_tool("generate_image", {"prompt": "a quiet sunrise"}, 0.01, active_tasks),
        timeout=2,
    )
    payload = json.loads(message.content or "{}")

    assert isinstance(message, InternalMessage)
    assert message.role == MessageRole.TOOL
    assert message.tool_call_id == "call-generate_image"
    assert payload["status"] == "queued"
    assert payload["tool_name"] == "generate_image"
    assert payload["task_id"] == 123
    assert len(submissions) == 1
    assert submissions[0][1]["tool_name"] == "generate_image"
    assert submissions[0][1]["arguments"] == {"prompt": "a quiet sunrise"}
    assert active_tasks == set()
