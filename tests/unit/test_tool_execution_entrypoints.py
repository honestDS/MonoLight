import inspect
from pathlib import Path

from app.core.dispatchers import interactive as interactive_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.dispatchers.interactive import InteractiveDispatcherMixin
from app.core.dispatchers.non_stream import NonStreamDispatcherMixin
from app.core.dispatchers.stream import StreamDispatcherMixin
from app.core.session_reply_queue import executor as executor_module
from app.core.tools import TOOL_EXECUTOR_MAP, tool_requires_audit
from app.core.utils.dispatcher import helpers as dispatcher_helpers
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module


def _assert_source_order(source: str, *markers: str) -> None:
    positions = [source.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_registered_tools_explicitly_declare_audit_requirement():
    assert all(isinstance(executor_class.requires_audit, bool) for executor_class in TOOL_EXECUTOR_MAP.values())
    assert {tool_name for tool_name in TOOL_EXECUTOR_MAP if tool_requires_audit(tool_name)} == {"execute_shell", "write_file"}
    assert all(not tool_requires_audit(tool_name) for tool_name in TOOL_EXECUTOR_MAP if tool_name not in {"execute_shell", "write_file"})
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
    )


def test_process_single_tool_has_no_old_auditor_or_compatibility_token():
    source = inspect.getsource(process_single_tool_module.process_single_tool)
    helper_source = inspect.getsource(dispatcher_helpers.process_single_tool_with_isolated_db)
    legacy_audit_call = "audit_" + "tool_call"
    compatibility_flag = "audit_" + "preapproved"
    legacy_audit_path = Path(__file__).parents[2] / "app/core/middleware/auditor.py"
    legacy_dispatcher_path = Path(__file__).parents[2] / "app/core/utils/dispatcher" / (legacy_audit_call + ".py")
    legacy_result_helper = "_" + "legacy_result_message_dedupe_key"

    assert legacy_audit_call not in source
    assert compatibility_flag not in source
    assert compatibility_flag not in helper_source
    assert legacy_audit_call not in vars(process_single_tool_module)
    assert compatibility_flag not in inspect.signature(process_single_tool_module.process_single_tool).parameters
    assert compatibility_flag not in inspect.signature(dispatcher_helpers.process_single_tool_with_isolated_db).parameters
    assert not legacy_audit_path.exists()
    assert not legacy_dispatcher_path.exists()
    assert not hasattr(executor_module, legacy_result_helper)


def test_old_confirmation_token_and_prefix_parsing_are_removed():
    app_core = Path(__file__).parents[2] / "app/core"
    source = "\n".join(path.read_text(encoding="utf-8") for path in app_core.rglob("*.py"))

    for legacy_marker in (
        "FORCE_EXECUTE_CONFIRMED",
        '"confirmation_required"',
        '"dynamic_token"',
        '"risky_command"',
        "LOG_AUDIT_TOKEN_",
    ):
        assert legacy_marker not in source
