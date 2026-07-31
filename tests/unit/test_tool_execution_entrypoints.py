import inspect

from app.core.dispatchers import interactive as interactive_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.dispatchers.interactive import InteractiveDispatcherMixin
from app.core.dispatchers.non_stream import NonStreamDispatcherMixin
from app.core.dispatchers.stream import StreamDispatcherMixin
from app.core.session_reply_queue import executor as executor_module
from app.core.tools import TOOL_EXECUTOR_MAP, tool_requires_audit


def _assert_source_order(source: str, *markers: str) -> None:
    positions = [source.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_registered_tools_explicitly_declare_audit_requirement():
    assert all(isinstance(executor_class.requires_audit, bool) for executor_class in TOOL_EXECUTOR_MAP.values())
    audited_tools = {"execute_shell", "write_file", "terminal_write", "terminal_close"}
    assert {tool_name for tool_name in TOOL_EXECUTOR_MAP if tool_requires_audit(tool_name)} == audited_tools
    assert all(not tool_requires_audit(tool_name) for tool_name in TOOL_EXECUTOR_MAP if tool_name not in audited_tools)
    assert tool_requires_audit("unknown_tool") is False


def test_interactive_stream_and_non_stream_share_audited_execution_entrypoint():
    interactive_source = inspect.getsource(InteractiveDispatcherMixin._dispatch_interactive)
    assert "if cfg.security.audit_channel_id and cfg.security.audit_model_id" not in interactive_source
    _assert_source_order(
        interactive_source,
        "prevalidate_tool_round(",
        "audit_tool_round(",
        "_execute_isolated_tool_call(",
    )
    assert "process_single_tool_with_isolated_db(" in inspect.getsource(interactive_module._execute_isolated_tool_call)

    assert "_dispatch_interactive(" in inspect.getsource(NonStreamDispatcherMixin.dispatch)
    assert "_run_dispatch(" in inspect.getsource(StreamDispatcherMixin.dispatch_stream)
    assert "_dispatch_interactive(" in inspect.getsource(StreamDispatcherMixin._run_dispatch)


def test_background_entrypoint_prechecks_before_batch_audit_and_execution():
    source = inspect.getsource(BackgroundDispatcherMixin._generate_reply_from_history)
    assert "if cfg.security.audit_channel_id and cfg.security.audit_model_id" not in source
    _assert_source_order(
        source,
        "validate_background_proactive_tool_calls(",
        "prevalidate_tool_round(",
        "audit_tool_round(",
        "process_single_tool_with_isolated_db(",
    )


def test_confirmed_entrypoint_reaudits_changed_files_before_precheck_and_execution():
    source = inspect.getsource(executor_module._execute_confirmed_tools)
    _assert_source_order(
        source,
        "audit_tool_round(",
        "prevalidate_tool_round(",
        "process_single_tool(",
        "_dispatch_interactive_work(",
    )
    interactive_source = inspect.getsource(executor_module._dispatch_interactive_work)
    assert "ChatDispatcher.dispatch(" in interactive_source
    assert "ChatDispatcher.dispatch_stream(" in interactive_source
