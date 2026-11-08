import json
from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy import select

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    GOAL_EXECUTION_PHASE_FINALIZING,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.exceptions import BaseBusinessException, LLMContextLengthException
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall, SubagentWork
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer


def _provider_error(error_kind: str) -> tuple[dict[str, Any], int]:
    if error_kind == "context_length":
        return {
            "error": {
                "code": "context_length_exceeded",
                "message": "maximum context length exceeded",
            }
        }, 400
    return {
        "error": {
            "type": "server_error",
            "message": "provider unavailable",
        }
    }, 503


def _error_capture(error_kind: str):
    provider_exceptions: list[BaseBusinessException] = []
    provider_error_messages: list[str] = []

    def raise_provider_error(transformer) -> None:
        raw_error, status = _provider_error(error_kind)
        try:
            transformer._raise_provider_error(
                raw_error,
                message=ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
                status=status,
            )
        except BaseBusinessException as exc:
            provider_exceptions.append(exc)
            provider_error_messages.append(exc.render_message())
            raise

    return raise_provider_error, provider_exceptions, provider_error_messages


async def _configure_runtime(runtime, *, include_end_session: bool = False) -> None:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        enabled_tools = [*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]
        if include_end_session:
            enabled_tools.append(END_SESSION_TOOL_NAME)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys(enabled_tools))
        profile.configs = configs

        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        session.stream_llm_request = False
        await db.commit()


async def _set_session_stream(runtime, enabled: bool) -> None:
    async with runtime.factory() as db:
        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        session.stream_llm_request = enabled
        await db.commit()


async def _add_fallback_rule(runtime, protocol: str) -> str:
    fallback_model_id = f"{runtime.chat_model_id}-fallback"
    async with runtime.factory() as db:
        channel = await db.get(ModelChannel, 1)
        assert channel is not None
        primary_entry = next(entry for entry in channel.model_ids or [] if entry.get("model_id") == runtime.chat_model_id and entry.get("usage") == "CHAT")
        channel.model_ids = [
            *channel.model_ids,
            {**primary_entry, "model_id": fallback_model_id, "protocol": protocol},
        ]

        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["channel"]["chat_channel"]["rules"].append(
            {
                "channel_id": 1,
                "model_id": fallback_model_id,
                "priority": 2,
                "weight": 1,
            }
        )
        profile.configs = configs
        await db.commit()
    return fallback_model_id


async def _context_final_trigger(runtime) -> int:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        channel = await db.get(ModelChannel, 1)
        assert profile is not None and channel is not None
        cfg = ProfileConfig.model_validate(profile.configs)
        model_entry = next(entry for entry in channel.model_ids or [] if entry.get("model_id") == runtime.chat_model_id and entry.get("usage") == "CHAT")
        return calculate_context_summary_trigger_tokens(
            context_window_k=model_entry["context_window_k"],
            max_tokens=model_entry["max_tokens"],
            threshold_percent=cfg.other.context_summary_threshold_percent,
        )


async def _start_parent(runtime) -> tuple[int, int, tuple[Any, Any]]:
    create_call = InternalToolCall(
        id="provider-failure-create-call",
        name=AGENT_WORK_TOOL_NAME,
        arguments={
            "operation": "create",
            "name": "provider failure child",
            "message": "Complete the isolated task and return the verified result.",
        },
    )
    runtime.outputs.extend(
        [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="parent accepted the child task"),
        ]
    )
    accepted = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": "subagent-provider-failure-parent-request",
            "message": "Create the child task and continue the parent task.",
        },
    )
    assert accepted.status_code == 200
    accepted_data = accepted.json()["data"]
    assert accepted_data["submission_status"] == "accepted"
    parent_work_id = accepted_data["work_id"]

    parent_done = await runtime.run_next(independent_only=False)
    assert parent_done is not None
    assert parent_done.id == parent_work_id
    assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED

    async with runtime.factory() as db:
        parent = await db.get(SessionReplyWorkItem, parent_work_id)
        assert parent is not None
        child = (
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                        SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                        SessionReplyWorkItem.source_id == str(parent_work_id),
                    )
                )
            )
            .scalars()
            .one()
        )
        assert child.id is not None
        assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
        calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
        assert len(calls) == 1
        assert calls[0].delivered is False
        assert calls[0].child_work_id == child.id
        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        return parent_work_id, child.id, (session.context_summary, session.context_summary_message_id)


async def _assert_failed_child_delivery(runtime, parent_work_id: int, child_work_id: int, expected_error: str) -> None:
    async with runtime.factory() as db:
        child = await db.get(SessionReplyWorkItem, child_work_id)
        assert child is not None
        assert child.status == SessionReplyWorkStatus.FAILED
        assert child.error == expected_error

        child_messages = (await db.execute(select(Message).where(Message.work_id == child_work_id).order_by(Message.id))).scalars().all()
        error_messages = [message for message in child_messages if message.role == MessageRole.ERR and message.type == MessageType.TEXT]
        assert len(error_messages) == 1
        assert error_messages[0].content == expected_error
        assert child.result_message_id == error_messages[0].id
        assert not [message for message in child_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]

        round_row = await db.get(SubagentWork, child_work_id)
        assert round_row is not None
        assert isinstance(round_row.result, dict)
        assert round_row.result["completed"] is True
        assert round_row.result["status"] == "failed"
        assert round_row.result["error"] == expected_error

        call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
        assert call.delivered is True
        assert call.tool_result_message_id is not None
        tool_result = await db.get(Message, call.tool_result_message_id)
        assert tool_result is not None
        assert tool_result.role == MessageRole.TOOL
        assert tool_result.type == MessageType.TOOL_RESULT
        tool_result_message = InternalMessage.model_validate_json(tool_result.content or "{}")
        assert tool_result_message.tool_execution_status == "failed"
        tool_result_payload = json.loads(tool_result_message.content or "{}")
        assert tool_result_payload["status"] == "failed"
        assert tool_result_payload["error"] == expected_error
        assert tool_result_payload["work"]["error"] == expected_error
        assert tool_result_payload["work"] == round_row.result

        parent = await db.get(SessionReplyWorkItem, parent_work_id)
        assert parent is not None
        assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM

    assert await runtime.run_next(independent_only=True) is None


async def _context_snapshot(runtime) -> tuple[Any, Any]:
    async with runtime.factory() as db:
        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        return session.context_summary, session.context_summary_message_id


async def _resume_parent(runtime, parent_work_id: int, expected_error: str) -> None:
    resume_start = len(runtime.provider_payloads)
    parent_content = "parent completed after receiving the child failure"
    runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_content))
    parent_done = await runtime.run_next(independent_only=False)
    assert parent_done is not None
    assert parent_done.id == parent_work_id
    assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED

    resume_payloads = runtime.provider_payloads[resume_start:]
    assert len(resume_payloads) == 1
    assert expected_error in json.dumps(resume_payloads, ensure_ascii=False)

    reply_response = await runtime.client.get(f"/api/v1/chat/reply-works/{parent_work_id}")
    assert reply_response.status_code == 200
    reply_data = reply_response.json()["data"]
    assert reply_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert reply_data["response"]["choices"][0]["message"]["content"] == parent_content

    history_response = await runtime.client.get(
        "/api/v1/chat/sessions/history",
        params={"session_id": runtime.session_id, "size": 100},
    )
    assert history_response.status_code == 200
    assert expected_error not in json.dumps(history_response.json()["data"], ensure_ascii=False)


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("phase", ["execution", "context_final"], ids=["execution", "context-final"])
@pytest.mark.parametrize("error_kind", ["provider", "context_length"], ids=["provider", "context-length"])
@pytest.mark.parametrize("upstream_stream", [False, True], ids=["non-stream", "stream"])
@pytest.mark.asyncio
async def test_subagent_provider_failure_is_delivered_as_failed_work(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    phase: str,
    error_kind: str,
    upstream_stream: bool,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        await _configure_runtime(runtime)
        parent_work_id, child_work_id, context_before = await _start_parent(runtime)
        fallback_model_id = await _add_fallback_rule(runtime, protocol)
        await _set_session_stream(runtime, upstream_stream)

        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        raise_provider_error, provider_exceptions, provider_error_messages = _error_capture(error_kind)
        child_payload_start = len(runtime.provider_payloads)

        async def fail_post_json(transformer, **kwargs):
            runtime.provider_payloads.append(kwargs["payload"])
            raise_provider_error(transformer)

        async def fail_stream_json(transformer, **kwargs):
            runtime.provider_payloads.append(kwargs["payload"])
            raise_provider_error(transformer)
            if False:
                yield {}

        context_trigger = await _context_final_trigger(runtime)

        def child_context_estimate(*_args, **_kwargs) -> int:
            return context_trigger if phase == "context_final" else 0

        with monkeypatch.context() as child_patch:
            child_patch.setattr(interactive_generation_module, "estimate_request_context_tokens", child_context_estimate)
            if upstream_stream:
                child_patch.setattr(transformer_cls, "_stream_sse_json", fail_stream_json)
            else:
                child_patch.setattr(transformer_cls, "_post_json", fail_post_json)
            child_done = await runtime.run_next(independent_only=True)

        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.FAILED
        expected_requests = 1 if phase == "context_final" else 2
        child_payloads = runtime.provider_payloads[child_payload_start:]
        assert len(child_payloads) == expected_requests
        assert [payload["model"] for payload in child_payloads] == ([runtime.chat_model_id] if expected_requests == 1 else [runtime.chat_model_id, fallback_model_id])
        assert len(provider_exceptions) == expected_requests
        assert len(provider_error_messages) == expected_requests
        assert len(set(provider_error_messages)) == 1
        if error_kind == "context_length":
            assert all(isinstance(exc, LLMContextLengthException) for exc in provider_exceptions)
        expected_error = provider_error_messages[-1]

        await _assert_failed_child_delivery(runtime, parent_work_id, child_work_id, expected_error)
        assert await _context_snapshot(runtime) == context_before
        async with runtime.factory() as db:
            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, child_work_id)
            assert isinstance(checkpoint, dict)
            if phase == "context_final":
                assert checkpoint["execution_phase"] == GOAL_EXECUTION_PHASE_FINALIZING
                assert checkpoint.get("subagent_context_exhausted") is True
            else:
                assert checkpoint.get("subagent_context_exhausted") is not True

        await _set_session_stream(runtime, False)
        await _resume_parent(runtime, parent_work_id, expected_error)
        assert await _context_snapshot(runtime) == context_before


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.asyncio
async def test_subagent_end_session_final_provider_failure_is_delivered_without_fallback(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        await _configure_runtime(runtime, include_end_session=True)
        parent_work_id, child_work_id, context_before = await _start_parent(runtime)
        fallback_model_id = await _add_fallback_rule(runtime, protocol)
        await _set_session_stream(runtime, False)

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="child-end-session", name=END_SESSION_TOOL_NAME, arguments={})],
            )
        )
        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        fixture_post_json = transformer_cls._post_json
        raise_provider_error, provider_exceptions, provider_error_messages = _error_capture("provider")
        child_payload_start = len(runtime.provider_payloads)
        end_session_requests = 0

        async def fail_after_end_session(transformer, **kwargs):
            nonlocal end_session_requests
            end_session_requests += 1
            if end_session_requests == 1:
                return await fixture_post_json(transformer, **kwargs)
            runtime.provider_payloads.append(kwargs["payload"])
            raise_provider_error(transformer)

        with monkeypatch.context() as child_patch:
            child_patch.setattr(transformer_cls, "_post_json", fail_after_end_session)
            child_done = await runtime.run_next(independent_only=True)

        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.FAILED
        child_payloads = runtime.provider_payloads[child_payload_start:]
        assert end_session_requests == 2
        assert len(child_payloads) == 2
        assert [payload["model"] for payload in child_payloads] == [runtime.chat_model_id, runtime.chat_model_id]
        assert fallback_model_id not in json.dumps(child_payloads, ensure_ascii=False)
        assert len(provider_exceptions) == 1
        assert len(provider_error_messages) == 1
        expected_error = provider_error_messages[0]

        await _assert_failed_child_delivery(runtime, parent_work_id, child_work_id, expected_error)
        assert await _context_snapshot(runtime) == context_before
        await _set_session_stream(runtime, False)
        await _resume_parent(runtime, parent_work_id, expected_error)
        assert await _context_snapshot(runtime) == context_before
