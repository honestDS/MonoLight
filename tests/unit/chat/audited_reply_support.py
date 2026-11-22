import json
from types import SimpleNamespace

from app.core.confirmation.service import ToolRoundResult
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import interactive_runtime as interactive_runtime_module
from app.core.dispatchers import interactive_tools as interactive_tools_module
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import OtherConfig, ToolConfig
from tests.unit.chat.reply_retry_context_support import (
    _Channel,
    _DatabaseLikeSession,
    _Dispatcher,
    _Session,
    _StreamDispatcher,
)

_DEFAULT_AUDIT_RESULT = object()


async def _run_audited_interactive_dispatch(
    monkeypatch,
    checkpoint_callback,
    process_tool,
    unknown_calls_target=None,
    finish_round_result=True,
    generated_calls_target=None,
    audit_result=_DEFAULT_AUDIT_RESULT,
    audit_waiter=None,
    claim_execution_success=True,
    stream_event_callback=None,
    stream_dispatch=False,
    show_tool_calls=True,
    additional_user_messages_fetcher=None,
    persist_pending_confirmation_bundle_handler=None,
    persist_cancelled_pending_audit_results_handler=None,
    supersede_pending_confirmation_bundle_handler=None,
    response_usages=None,
    execution_resume_state=None,
    response_messages=None,
    audit_calls_target=None,
    audit_results=None,
    generate_hook=None,
    finish_attempt_calls_target=None,
    finish_round_if_complete_calls_target=None,
    finish_round_if_complete_result=None,
    use_execution_round_if_complete=False,
    tool_call=None,
    multimodal_capabilities=None,
    summary_calls_target=None,
    trim_calls_target=None,
    persist_todo_snapshot_handler=None,
):
    profile = SimpleNamespace(id=1)
    audit_configured = audit_results is not None or audit_result is not None
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        security=SimpleNamespace(
            audit_channel_id=1 if audit_configured else None,
            audit_model_id="audit-model" if audit_configured else None,
        ),
        tool=ToolConfig(max_parallel_tools=5, executor_max_workers=1),
        other=OtherConfig(),
    )
    tool_call = tool_call or InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": "echo 1"},
    )
    responses = (
        list(response_messages)
        if response_messages is not None
        else [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="finished"),
        ]
    )
    saved_message_id = 10
    response_usage_iterator = iter(response_usages or [])
    audit_results_iterator = iter(audit_results) if audit_results is not None else None
    next_audit_record_id = 42
    audit_details_by_record_id = {}
    executable_audit_record_ids = set()

    async def get_user(db, uid):
        return SimpleNamespace(username="operator")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        return _Channel(), {"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile, *, actor_uid=None, goal_mode=False, tool_workflow=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ], []

    async def mark_initial_message_processed(db, initial_message_id):
        return None

    async def generate(**kwargs):
        if generated_calls_target is not None:
            generated_calls_target.append(
                {
                    **kwargs,
                    "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
                }
            )
        response_message = responses.pop(0)
        if generate_hook is not None:
            await generate_hook(response_message)
        return InternalResponse(
            message=response_message,
            model=kwargs["model_id"],
            usage=next(response_usage_iterator, {}),
        )

    async def generate_with_stream_callback(**kwargs):
        response = await generate(**kwargs)
        reasoning_content = response.message.reasoning_content
        if isinstance(reasoning_content, str) and reasoning_content:
            await kwargs["on_reasoning"](reasoning_content)
        return response

    async def save_assistant(*args, **kwargs):
        nonlocal saved_message_id
        message = args[4]
        saved_message_id += 1
        return SimpleNamespace(id=saved_message_id, content=message.content)

    async def save_tool_response(db, session_id, uid, profile_id, tool_result, messages, turn_messages):
        tool_result.id = 200
        messages.append(tool_result)
        turn_messages.append(tool_result)
        return tool_result

    async def audit_round(*args, **kwargs):
        nonlocal next_audit_record_id
        audit_record_id = next_audit_record_id
        next_audit_record_id += 1
        round_tool_calls = [item.model_copy(deep=True) for item in kwargs["tool_calls"]]
        if audit_calls_target is not None:
            audit_calls_target.append(round_tool_calls)
        audit_details_by_record_id[audit_record_id] = [SimpleNamespace(original_tool_call_id=tool_call.id, id=(audit_record_id * 100) + index) for index, tool_call in enumerate(round_tool_calls, start=1)]
        if audit_waiter is not None:
            await audit_waiter()
        selected_audit_result = audit_result
        if audit_results_iterator is not None:
            try:
                selected_audit_result = next(audit_results_iterator)
            except StopIteration:
                pass
        if selected_audit_result is _DEFAULT_AUDIT_RESULT:
            selected_audit_result = ToolRoundResult(
                audit_record_id=audit_record_id,
                status=AuditRecordStatus.PASSED,
                tool_results=(),
                confirmation_payload=None,
            )
        if selected_audit_result is not None:
            audit_details_by_record_id[selected_audit_result.audit_record_id] = audit_details_by_record_id[audit_record_id]
            if selected_audit_result.may_execute:
                executable_audit_record_ids.add(selected_audit_result.audit_record_id)
        return selected_audit_result

    async def persist_confirmation_bundle(
        db,
        *,
        tool_results,
        confirmation_payload,
        **kwargs,
    ):
        stored_results = []
        for index, tool_result in enumerate(tool_results, start=1):
            stored_result = tool_result.model_copy(deep=True)
            stored_result.id = 200 + index
            stored_results.append(stored_result)
        confirmation_message = InternalMessage(
            id=300,
            role=MessageRole.ASSISTANT,
            content=json.dumps(confirmation_payload, ensure_ascii=False),
        )
        return stored_results, confirmation_message

    async def claim_execution(db, *, audit_record_id):
        if audit_record_id not in executable_audit_record_ids:
            raise AssertionError("non-executable audit must not create a claim")
        if not claim_execution_success:
            return None, None
        return SimpleNamespace(execution_claim_token="claim-token"), "claim-token"

    async def list_details(db, audit_record_id):
        return audit_details_by_record_id[audit_record_id]

    async def create_execution(db, **kwargs):
        if kwargs["audit_record_id"] not in executable_audit_record_ids:
            raise AssertionError("non-executable audit must not create execution records")
        return SimpleNamespace(id=8)

    async def finish_attempt(db, **kwargs):
        if finish_attempt_calls_target is not None:
            finish_attempt_calls_target.append(dict(kwargs))
        return True

    async def finish_round(db, **kwargs):
        return finish_round_result

    async def finish_round_if_complete(db, **kwargs):
        if finish_round_if_complete_calls_target is not None:
            finish_round_if_complete_calls_target.append(dict(kwargs))
        return finish_round_if_complete_result

    async def update_confirmation(db, *, audit_record_id):
        return None

    unknown_calls = unknown_calls_target if unknown_calls_target is not None else []

    async def mark_unknown(db, **kwargs):
        unknown_calls.append(kwargs)
        return True

    monkeypatch.setattr(interactive_runtime_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_runtime_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_runtime_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_runtime_module, "mark_initial_message_processed", mark_initial_message_processed)
    monkeypatch.setattr(interactive_runtime_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_generation_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_runtime_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(
        interactive_runtime_module,
        "get_multimodal_from_entry",
        lambda model_entry: multimodal_capabilities or (False, False, False),
    )
    monkeypatch.setattr(
        interactive_generation_module,
        "get_multimodal_from_entry",
        lambda model_entry: multimodal_capabilities or (False, False, False),
    )
    monkeypatch.setattr(
        interactive_runtime_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(
        interactive_generation_module,
        "resolve_chat_params",
        lambda model_entry, channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )

    async def prepare_messages(*args, **kwargs):
        return [InternalMessage(role=MessageRole.USER, content="request")]

    def materialize_environment_prompt(messages):
        if multimodal_capabilities is not None:
            return [message.model_copy(deep=True) for message in messages]
        return messages

    async def apply_context_summary_checkpoint(db, **kwargs):
        if summary_calls_target is not None:
            summary_calls_target.append(dict(kwargs))
        return kwargs["messages"]

    def prepare_messages_for_model_request(**kwargs):
        if trim_calls_target is not None:
            trim_calls_target.append(dict(kwargs))
        return kwargs["messages"]

    monkeypatch.setattr(interactive_runtime_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(interactive_generation_module, "apply_context_summary_checkpoint", apply_context_summary_checkpoint)
    monkeypatch.setattr(interactive_generation_module, "materialize_user_environment_prompts", materialize_environment_prompt)
    monkeypatch.setattr(interactive_generation_module.ContextManager, "prepare_messages_for_model_request", prepare_messages_for_model_request)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(interactive_runtime_module, "save_assistant_message", save_assistant)
    monkeypatch.setattr(
        interactive_tools_module,
        "persist_pending_confirmation_bundle",
        persist_pending_confirmation_bundle_handler or persist_confirmation_bundle,
    )
    if persist_cancelled_pending_audit_results_handler is not None:
        monkeypatch.setattr(
            interactive_tools_module,
            "persist_cancelled_pending_audit_results",
            persist_cancelled_pending_audit_results_handler,
        )
    if supersede_pending_confirmation_bundle_handler is not None:
        monkeypatch.setattr(
            interactive_tools_module,
            "supersede_persisted_pending_confirmation_bundle",
            supersede_pending_confirmation_bundle_handler,
        )
    monkeypatch.setattr(interactive_tools_module, "save_tool_response", save_tool_response)

    async def persist_todo_snapshot(_db, *, uid, session_id, tool_results):
        return None

    monkeypatch.setattr(
        interactive_tools_module,
        "persist_session_todo_snapshot_on_tool_results",
        persist_todo_snapshot_handler or persist_todo_snapshot,
    )
    monkeypatch.setattr(interactive_tools_module, "prepare_tool_round", audit_round)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "claim_passed_for_execution", claim_execution)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "list_tool_details", list_details)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "create_execution_attempt", create_execution)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "finish_execution_attempt", finish_attempt)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "finish_execution_round", finish_round)
    if use_execution_round_if_complete:
        monkeypatch.setattr(interactive_tools_module.audit_crud, "finish_execution_round_if_complete", finish_round_if_complete)
    monkeypatch.setattr(interactive_tools_module.audit_crud, "mark_execution_unknown", mark_unknown)
    monkeypatch.setattr(interactive_tools_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_helpers_module, "update_confirmation_message_status", update_confirmation)
    monkeypatch.setattr(interactive_tools_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(interactive_helpers_module, "process_single_tool_with_isolated_db", process_tool)

    async def ensure_runtime_snapshot(_db, _session_id, message, _max_tokens):
        if not message.environment_prompt:
            message.environment_prompt = "runtime snapshot"
        return message

    monkeypatch.setattr(interactive_helpers_module, "ensure_user_runtime_instructions", ensure_runtime_snapshot)

    async def get_session_by_id(*args, **kwargs):
        return SimpleNamespace(
            goal_mode=False,
            source="http",
            max_turns=5,
            context_summary_revision=0,
            context_content_revision=0,
            llm_request_metadata=None,
            stream_llm_request=None,
        )

    db = _DatabaseLikeSession() if use_execution_round_if_complete else _Session()
    if use_execution_round_if_complete:
        monkeypatch.setattr(interactive_generation_module.session_crud, "get_by_session_id", get_session_by_id)

    if stream_dispatch:
        response = [
            event
            async for event in _StreamDispatcher.dispatch_stream(
                db=db,
                message="request",
                uid="user-1",
                session_id="session-1",
                request_id="request-1",
                persisted_initial_message=InternalMessage(id=1, role=MessageRole.USER, content="request"),
                frozen_user_message_ids=[1],
                persisted_profile_id=1,
                execution_checkpoint_callback=checkpoint_callback,
                additional_user_messages_fetcher=additional_user_messages_fetcher,
                show_tool_calls=show_tool_calls,
            )
        ]
    else:
        response = await _Dispatcher.dispatch(
            db=db,
            message="request",
            uid="user-1",
            session_id="session-1",
            persisted_initial_message=InternalMessage(id=1, role=MessageRole.USER, content="request"),
            frozen_user_message_ids=[1],
            persisted_profile_id=1,
            execution_checkpoint_callback=checkpoint_callback,
            stream_event_callback=stream_event_callback,
            additional_user_messages_fetcher=additional_user_messages_fetcher,
            execution_resume_state=execution_resume_state,
            show_tool_calls=show_tool_calls,
        )
    return response, unknown_calls
