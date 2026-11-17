from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode
from app.core.utils.context_summary.history import build_summary_fragment_plan, iter_summary_fragments
from app.core.utils.context_summary.model_call import estimate_context_summary_prompt_tokens
from app.core.utils.context_summary.pipeline import SummaryFragmentInput
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType, StoredConversationSummary
from app.models.profile import OtherConfig, ProfileConfig
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from app.providers.llm.client import LLMClient, estimate_request_context_tokens
from app.transformers.openai.base import BaseOpenAITransformer
from tests.integration.context.context_summary_workflow_support import _context_summary_database as _context_summary_database

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")
http_transport_module = import_module("app.transformers.http_transport")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_mode", ["threshold", "forced"], ids=["threshold", "forced"])
@pytest.mark.parametrize(
    "main_protocol",
    ["openai", "openai_responses", "deepseek", "deepseek_responses"],
    ids=["openai", "openai-responses", "deepseek", "deepseek-responses"],
)
async def test_context_summary_chunks_tool_results_and_preserves_continuation(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    entry_mode: str,
    main_protocol: str,
) -> None:
    uid = "continuation-user"
    session_id = "continuation-session"
    main_channel_id = 21
    main_model_id = "main-model"
    summary_model = ContextSummaryModelSnapshot(
        channel_id=11,
        channel_name="small-summary-channel",
        model_id="small-summary-model",
        protocol="openai",
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=4_096,
        max_output_tokens=512,
        temperature=0.0,
        top_p=None,
        safety_margin_tokens=0,
        input_budget_tokens=3_584,
    )
    evidence_markers = ["TOOL_EVIDENCE_ALPHA_2F", "TOOL_EVIDENCE_BETA_7C", "TOOL_EVIDENCE_GAMMA_9A"]
    continuation_marker = "TOOL_EVIDENCE_DELTA_4D"
    db_user_content = "Continue the verified task from the first objective."
    full_user_content = f"{db_user_content}\n\nThe second objective must preserve every verified evidence marker."
    guidance_prompt = "guidance-continuation-marker"
    user_environment_prompt = json.dumps(
        {
            "environment": {"runtime_context": "runtime-continuation-marker"},
            "response_settings": {"markdown": True, "max_output_tokens": 512},
            "platform_constraints": "platform-continuation-marker",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    todo_v1 = [{"content": "preserve the verified tool evidence", "status": "in_progress"}]
    todo_v2 = [
        {"content": "preserve the verified tool evidence", "status": "in_progress"},
        {"content": "continue with the next tool result", "status": "pending"},
    ]
    tool_calls = [
        InternalToolCall(
            id=f"continuation-call-{index}",
            name="collect_evidence",
            arguments={"evidence_index": index},
        )
        for index in range(1, 4)
    ]
    tool_call_message = InternalMessage(
        id=2,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        tool_calls=tool_calls,
    )
    tool_results: list[InternalMessage] = []
    tool_result_rows: list[Message] = []
    for index, (tool_call, marker) in enumerate(zip(tool_calls, evidence_markers), start=3):
        result_content = json.dumps(
            {"result": ("x " * 3_200) + marker},
            ensure_ascii=False,
        )
        result_message = InternalMessage(
            id=index,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=result_content,
            tool_call_id=tool_call.id,
            tool_execution_status="succeeded",
        )
        tool_results.append(result_message)
        tool_result_rows.append(
            Message(
                id=index,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                content=result_message.model_dump_json(),
                is_processed=True,
            )
        )

    single_result_limit_tokens = 4_000
    result_token_estimates = [estimate_tokens(result.content or "", model_id=main_model_id, protocol=main_protocol) for result in tool_results]
    assert all(token_count < single_result_limit_tokens for token_count in result_token_estimates)
    assert all((len(result.content or "") + 3) // 4 < single_result_limit_tokens for result in tool_results)
    assert sum(result_token_estimates) > 8_000 - 512

    provider_metadata = {
        "input_tokens": 7_000,
        "input_tokens_source": "provider",
        "channel_id": main_channel_id,
        "model_id": main_model_id,
        "protocol": main_protocol,
        "context_summary_revision": 0,
        "context_content_revision": 0,
    }
    if entry_mode == "threshold":
        assert provider_metadata["input_tokens"] >= calculate_context_summary_trigger_tokens(
            context_window_k=8,
            max_tokens=512,
            threshold_percent=90,
        )
    async with context_summary_database() as db:
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                llm_request_metadata=provider_metadata if entry_mode == "threshold" else None,
            )
        )
        db.add(
            SessionTodoPlan(
                session_id=session_id,
                uid=uid,
                revision=1,
                todos=todo_v1,
            )
        )
        db.add(
            Message(
                id=1,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.USER,
                content=db_user_content,
                environment_prompt=user_environment_prompt,
                guidance_prompt=guidance_prompt,
                is_processed=True,
            )
        )
        db.add(
            Message(
                id=2,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                content=tool_call_message.model_dump_json(),
                is_processed=True,
            )
        )
        db.add_all(tool_result_rows)
        await db.commit()

    summary_prompts: list[str] = []
    all_markers = [*evidence_markers, continuation_marker]

    async def _select_model(*_args, **_kwargs) -> ContextSummaryModelSnapshot:
        return summary_model

    async def _call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        assert model == summary_model
        assert estimate_context_summary_prompt_tokens(model, prompt) <= model.input_budget_tokens
        summary_prompts.append(prompt)
        prompt_payload = json.loads(prompt)
        prompt_markers = [marker for marker in all_markers if marker in json.dumps(prompt_payload, ensure_ascii=False)]
        if "message_chunks" in prompt_payload:
            prefix = "raw evidence context"
        else:
            prefix = "merged"
        return " ".join([prefix, *prompt_markers])

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_service, "select_context_summary_model", _select_model)
    monkeypatch.setattr(context_summary_stage, "call_context_summary_model", _call_model)

    cfg = SimpleNamespace(
        channel=SimpleNamespace(context_summary_channel=object()),
        other=OtherConfig(context_summary_threshold_percent=90),
    )
    profile = SimpleNamespace(id=1)

    async def _run_checkpoint(
        messages: list[InternalMessage],
        *,
        force: bool,
        previous_metadata: dict | None = None,
    ) -> list[InternalMessage]:
        async with context_summary_database() as db:
            return await apply_context_summary_checkpoint(
                db,
                session_id=session_id,
                uid=uid,
                profile=profile,
                cfg=cfg,
                messages=messages,
                trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                fixed_upper_message_id=1,
                context_window_k=8,
                max_tokens=512,
                tools=[],
                channel_id=main_channel_id,
                model_id=main_model_id,
                protocol=main_protocol,
                previous_llm_request_metadata=previous_metadata,
                force=force,
            )

    def _summary_message(messages: list[InternalMessage]) -> InternalMessage:
        summaries = [message for message in messages if message.role == MessageRole.USER and message.conversation_summary is not None]
        assert len(summaries) == 1
        return summaries[0]

    def _assert_summary_content(
        content: str | None,
        *,
        expected_todo_revision: int,
        expected_todos: list[dict[str, str]],
        expected_markers: list[str],
    ) -> None:
        assert content is not None
        stored_summary = StoredConversationSummary.model_validate_json(content)
        assert stored_summary.user_message == full_user_content
        assert stored_summary.covered_user_message_id == 1
        assert all(marker in stored_summary.conversation_summary for marker in expected_markers)
        assert stored_summary.current_session_todo == {
            "revision": expected_todo_revision,
            "todos": expected_todos,
        }

    def _assert_summary_message(
        message: InternalMessage,
        *,
        through_message_id: int,
        expected_todo_revision: int,
        expected_todos: list[dict[str, str]],
        expected_markers: list[str],
    ) -> None:
        summary_meta = message.conversation_summary
        assert summary_meta is not None
        assert summary_meta.through_message_id == through_message_id
        assert summary_meta.covered_user_message_id == 1
        assert message.content == full_user_content
        assert all(marker in summary_meta.content for marker in expected_markers)
        assert message.current_session_todo == {
            "revision": expected_todo_revision,
            "todos": expected_todos,
        }
        materialized_message = materialize_user_environment_prompts([message])[0]
        assert isinstance(materialized_message.content, str)
        materialized_payload = json.loads(materialized_message.content)
        assert materialized_payload["conversation_summary"] == summary_meta.content
        assert materialized_payload["user_message"] == full_user_content
        assert materialized_payload["current_session_todo"] == message.current_session_todo
        assert isinstance(message.environment_prompt, str)
        environment = json.loads(message.environment_prompt)
        assert environment["environment"]["runtime_context"] == "runtime-continuation-marker"
        assert environment["platform_constraints"] == "platform-continuation-marker"

    initial_messages = [
        InternalMessage(
            id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=full_user_content,
            environment_prompt=user_environment_prompt,
            guidance_prompt=guidance_prompt,
        ),
        tool_call_message,
        *tool_results,
    ]
    first_checkpoint = await _run_checkpoint(
        initial_messages,
        force=entry_mode == "forced",
        previous_metadata=provider_metadata if entry_mode == "threshold" else None,
    )
    first_prompt_count = len(summary_prompts)
    first_prompts = summary_prompts[:first_prompt_count]
    assert first_prompt_count > len(tool_results)
    chunks_by_message_id: dict[int, list[tuple[int, str]]] = {}
    for prompt in first_prompts:
        for chunk in json.loads(prompt).get("message_chunks", []):
            if not isinstance(chunk, dict) or chunk.get("role") != MessageRole.TOOL.value or not isinstance(chunk.get("message_id"), int) or not isinstance(chunk.get("part"), int) or not isinstance(chunk.get("content"), str):
                continue
            message_id = chunk["message_id"]
            chunks_by_message_id.setdefault(message_id, []).append((chunk["part"], chunk["content"]))

    assert set(chunks_by_message_id) == {result.id for result in tool_results}
    for result in tool_results:
        assert result.id is not None
        ordered_chunks = sorted(chunks_by_message_id[result.id])
        parts = [part for part, _ in ordered_chunks]
        assert len(parts) == len(set(parts))
        assert parts == list(range(len(parts)))
        parsed = json.loads("".join(payload for _, payload in ordered_chunks))
        assert parsed["content"] == result.content
        assert parsed["tool_call_id"] == result.tool_call_id
    assert len(first_checkpoint) == 1
    first_summary_message = _summary_message(first_checkpoint)
    _assert_summary_message(
        first_summary_message,
        through_message_id=5,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))).scalar_one()
        first_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
    assert session.context_summary_message_id == 5
    assert session.context_summary_revision == 1
    _assert_summary_content(
        session.context_summary,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )
    assert [row.content for row in first_rows] == [
        db_user_content,
        tool_call_message.model_dump_json(),
        *(result.model_dump_json() for result in tool_results),
    ]
    first_parsed = parse_db_messages_to_internal(first_rows)
    assert [message.id for message in first_parsed] == [1, 2, 3, 4, 5]
    assert first_parsed[0].content == db_user_content
    assert [message.tool_call_id for message in first_parsed[2:]] == [tool_call.id for tool_call in tool_calls]
    assert [message.content for message in first_parsed[2:]] == [result.content for result in tool_results]

    async def _build_system_prompt(_db, _profile, **_kwargs):
        return "stable system prompt"

    monkeypatch.setattr(prepare_module, "build_system_prompt", _build_system_prompt)
    async with context_summary_database() as db:
        reloaded_messages = await prepare_module.prepare_messages(
            db,
            session_id,
            uid,
            profile,
            cfg,
            None,
            "",
            False,
            context_window_k=8,
            max_tokens=512,
            processed_only=True,
        )
    assert len(reloaded_messages) == 2
    assert reloaded_messages[0].role == MessageRole.SYSTEM
    assert reloaded_messages[1].role == MessageRole.USER
    assert [message.id for message in reloaded_messages if message.id is not None] == []
    reloaded_summary = reloaded_messages[1:]
    assert len(reloaded_summary) == 1
    _assert_summary_message(
        reloaded_summary[0],
        through_message_id=5,
        expected_todo_revision=1,
        expected_todos=todo_v1,
        expected_markers=evidence_markers,
    )
    assert reloaded_summary[0].content == first_summary_message.content
    materialized_reloaded_messages = materialize_user_environment_prompts(reloaded_messages)
    assert isinstance(materialized_reloaded_messages[1].content, str)
    reloaded_user_payload = json.loads(materialized_reloaded_messages[1].content)
    assert reloaded_user_payload["conversation_summary"] == reloaded_summary[0].conversation_summary.content
    assert reloaded_user_payload["user_message"] == full_user_content
    assert reloaded_user_payload["environment"]["runtime_context"] == "runtime-continuation-marker"
    assert reloaded_user_payload["platform_constraints"] == "platform-continuation-marker"
    assert reloaded_user_payload["response_settings"] == {"markdown": True, "max_output_tokens": 512}
    assert reloaded_user_payload["platform_guidance"] == guidance_prompt

    main_upstream_payloads: list[dict] = []

    async def capture_upstream_json(*_args, **kwargs):
        main_upstream_payloads.append(kwargs["payload"])
        if main_protocol in {"openai", "deepseek"}:
            return {
                "id": "chatcmpl-main",
                "model": main_model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "main response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        return {
            "id": "resp-main",
            "status": "completed",
            "model": main_model_id,
            "output": [
                {
                    "id": "msg-main",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "main response", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", capture_upstream_json)
    monkeypatch.setattr(http_transport_module, "post_json", capture_upstream_json)
    main_response = await LLMClient.generate(
        api_key="test-key",
        base_url="https://main.invalid/v1",
        model_id=main_model_id,
        messages=materialized_reloaded_messages,
        tools=[],
        max_tokens=512,
        channel_id=main_channel_id,
        protocol=main_protocol,
    )

    assert main_response.message.content == "main response"
    assert len(main_upstream_payloads) == 1
    main_payload = main_upstream_payloads[0]
    assert main_payload["model"] == main_model_id
    input_key = "input" if main_protocol in {"openai_responses", "deepseek_responses"} else "messages"
    assert input_key in main_payload
    outbound_input = main_payload[input_key]
    assert isinstance(outbound_input, list)
    outbound_user_items = [item for item in outbound_input if isinstance(item, dict) and item.get("role") == MessageRole.USER.value]
    assert len(outbound_user_items) == 1
    outbound_user_content = outbound_user_items[0].get("content")
    assert isinstance(outbound_user_content, str)
    outbound_user_payload = json.loads(outbound_user_content)
    assert outbound_user_payload == reloaded_user_payload
    assert isinstance(outbound_user_payload["conversation_summary"], str)
    assert isinstance(outbound_user_payload["user_message"], str)
    assert "through_message_id" not in outbound_user_content
    assert "covered_user_message_id" not in outbound_user_content
    assert "<covered_user_message" not in outbound_user_content

    input_payload = {input_key: outbound_input}
    outbound_input_tokens = estimate_tokens(
        json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"), default=str),
        model_id=main_model_id,
        protocol=main_protocol,
    )
    estimated_input_tokens = estimate_request_context_tokens(
        materialized_reloaded_messages,
        [],
        model_id=main_model_id,
        protocol=main_protocol,
        channel_id=main_channel_id,
    )
    assert outbound_input_tokens == estimated_input_tokens

    new_tool_call = InternalToolCall(
        id="continuation-call-delta",
        name="collect_evidence",
        arguments={"evidence_index": 4},
    )
    new_tool_call_message = InternalMessage(
        id=6,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        tool_calls=[new_tool_call],
    )
    new_result = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=json.dumps(
            {"result": f"continuation output with {continuation_marker}"},
            ensure_ascii=False,
        ),
        tool_call_id=new_tool_call.id,
        tool_execution_status="succeeded",
    )
    async with context_summary_database() as db:
        plan = await db.get(SessionTodoPlan, session_id)
        assert plan is not None
        plan.revision = 2
        plan.todos = todo_v2
        db.add(
            Message(
                id=6,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                content=new_tool_call_message.model_dump_json(),
                is_processed=True,
            )
        )
        db.add(
            Message(
                id=7,
                session_id=session_id,
                uid=uid,
                profile_id=1,
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                content=new_result.model_dump_json(),
                is_processed=True,
            )
        )
        await db.commit()

    second_prompt_start = len(summary_prompts)
    second_checkpoint = await _run_checkpoint(
        [*reloaded_summary, new_tool_call_message, new_result],
        force=True,
    )
    second_prompts = summary_prompts[second_prompt_start:]
    assert second_prompts
    second_prompt_payloads = [json.loads(prompt) for prompt in second_prompts]
    assert any(continuation_marker in json.dumps(payload["message_chunks"], ensure_ascii=False) for payload in second_prompt_payloads if "message_chunks" in payload)
    assert len(second_checkpoint) == 1
    second_summary_message = _summary_message(second_checkpoint)
    _assert_summary_message(
        second_summary_message,
        through_message_id=7,
        expected_todo_revision=2,
        expected_todos=todo_v2,
        expected_markers=[*evidence_markers, continuation_marker],
    )
    assert second_summary_message.environment_prompt == first_summary_message.environment_prompt

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))).scalar_one()
    assert session.context_summary_message_id == 7
    assert session.context_summary_revision == 2
    _assert_summary_content(
        session.context_summary,
        expected_todo_revision=2,
        expected_todos=todo_v2,
        expected_markers=[*evidence_markers, continuation_marker],
    )

    stable_prompt_count = len(summary_prompts)
    stable_checkpoint = await _run_checkpoint(second_checkpoint, force=True)
    assert len(summary_prompts) == stable_prompt_count
    assert len(stable_checkpoint) == 1
    assert stable_checkpoint[0].content == second_summary_message.content
    assert stable_checkpoint[0].environment_prompt == second_summary_message.environment_prompt

    async with context_summary_database() as db:
        final_rows = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars())
        final_stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.session_id == session_id))).scalars())
        final_fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.session_id == session_id))).scalars())
    assert final_stages == []
    assert final_fragments == []
    assert [row.id for row in final_rows] == [1, 2, 3, 4, 5, 6, 7]
    assert [row.content for row in final_rows] == [
        db_user_content,
        tool_call_message.model_dump_json(),
        *(result.model_dump_json() for result in tool_results),
        new_tool_call_message.model_dump_json(),
        new_result.model_dump_json(),
    ]
    final_parsed = parse_db_messages_to_internal(final_rows)
    assert [message.id for message in final_parsed] == [1, 2, 3, 4, 5, 6, 7]
    assert final_parsed[0].content == db_user_content
    final_tool_call = next(message for message in final_parsed if message.id == 6)
    assert [tool_call.id for tool_call in final_tool_call.tool_calls or []] == [new_tool_call.id]
    final_tool_results = [message for message in final_parsed if message.role == MessageRole.TOOL]
    assert [message.id for message in final_tool_results] == [3, 4, 5, 7]
    assert [message.tool_call_id for message in final_tool_results] == [tool_call.id for tool_call in [*tool_calls, new_tool_call]]
    assert [message.content for message in final_tool_results] == [result.content for result in [*tool_results, new_result]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_shape",
    ["single-message", "grouped-messages"],
    ids=["single-message", "grouped-messages"],
)
@pytest.mark.parametrize(
    ("model_id", "protocol"),
    [
        pytest.param("gemini-3.8-flash-low", "openai", id="gemini-openai"),
        pytest.param("gpt-5.6-luna", "openai_responses", id="gpt-responses"),
    ],
)
async def test_context_summary_first_layer_uses_complete_request_budget_for_escaped_history(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    source_shape: str,
    model_id: str,
    protocol: str,
) -> None:
    uid = "escaped-summary-user"
    session_id = f"escaped-summary-{source_shape}"
    escaped_row = 'row\n"C:\\test\\file"\t'

    def _source_content(repeats: int, marker: str) -> str:
        return json.dumps(
            {"result": f"{marker}: {escaped_row * repeats}"},
            ensure_ascii=False,
        )

    if source_shape == "single-message":
        entries = [
            (MessageRole.USER, _source_content(900, "single-user")),
            (MessageRole.ASSISTANT, "assistant reply"),
        ]
    else:
        assert source_shape == "grouped-messages"
        entries = []
        for round_index in range(12):
            entries.extend(
                [
                    (MessageRole.USER, _source_content(90, f"round-{round_index}-user")),
                    (MessageRole.ASSISTANT, _source_content(90, f"round-{round_index}-assistant")),
                ]
            )

    message_rows = [
        Message(
            id=message_id,
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=role,
            content=content,
        )
        for message_id, (role, content) in enumerate(entries, start=1)
    ]
    target_message_id = len(message_rows)
    original_contents = {message.id: message.content for message in message_rows}

    async with context_summary_database() as db:
        db.add(ChatSession(session_id=session_id, uid=uid))
        db.add_all(message_rows)
        await db.commit()

    model = ContextSummaryModelSnapshot(
        channel_id=11,
        channel_name="escaped-summary",
        model_id=model_id,
        protocol=protocol,
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=4_096,
        max_output_tokens=512,
        temperature=0.0,
        top_p=None,
        safety_margin_tokens=0,
        input_budget_tokens=3_584,
    )

    empty_fragment_prompt = context_summary_stage.fragment_prompt(
        SummaryFragmentInput(
            fragment_index=0,
            message_start_id=1,
            message_end_id=target_message_id,
            token_count=1,
            content=json.dumps({"message_chunks": []}, ensure_ascii=False, separators=(",", ":")),
        )
    )
    old_max_fragment_tokens = model.input_budget_tokens - estimate_context_summary_prompt_tokens(model, empty_fragment_prompt) - 32
    assert old_max_fragment_tokens > 0

    async with context_summary_database() as db:
        snapshot = await build_context_summary_snapshot(
            db,
            session_id=session_id,
            uid=uid,
            expected_summary_message_id=None,
            before_id=None,
            target_message_id=target_message_id,
            content_revision=0,
        )
        old_plan = await build_summary_fragment_plan(
            db,
            session_id=session_id,
            uid=uid,
            snapshot=snapshot,
            max_fragment_tokens=old_max_fragment_tokens,
            model_id=model.model_id,
            protocol=model.protocol,
        )
        old_fragments = [
            fragment
            async for fragment in iter_summary_fragments(
                db,
                session_id=session_id,
                uid=uid,
                snapshot=snapshot,
                existing_summary=None,
                plan=old_plan,
                max_fragment_tokens=old_max_fragment_tokens,
                model_id=model.model_id,
                protocol=model.protocol,
            )
        ]

    old_prompt_tokens = [estimate_context_summary_prompt_tokens(model, context_summary_stage.fragment_prompt(fragment)) for fragment in old_fragments]
    assert old_fragments
    assert any(prompt_tokens > model.input_budget_tokens for prompt_tokens in old_prompt_tokens)

    model_requests: list[tuple[str, str, int]] = []

    async def _call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        assert model == summary_model
        prompt_tokens = estimate_context_summary_prompt_tokens(model, prompt)
        assert prompt_tokens <= model.input_budget_tokens
        model_requests.append((model.model_id, prompt, prompt_tokens))
        return "summary"

    async def _select_model(*_args, **_kwargs) -> ContextSummaryModelSnapshot:
        return summary_model

    summary_model = model
    monkeypatch.setattr(context_summary_stage, "call_context_summary_model", _call_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", _select_model)

    async with context_summary_database() as db:
        generated = await context_summary_stage.generate_snapshot_summary_with_model(
            db,
            session_id=session_id,
            uid=uid,
            profile=SimpleNamespace(id=1),
            cfg=ProfileConfig.model_validate({}),
            snapshot=snapshot,
            existing_summary=None,
            existing_summary_revision=0,
            safety_margin_tokens=0,
            model=model,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage).order_by(ContextSummaryStage.id))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment).order_by(ContextSummaryFragment.id))).scalars().all())
        persisted_messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())

    assert generated.content == "summary"
    assert generated.message_count == len(message_rows)
    assert generated.completed_stage is not None
    assert model_requests
    assert all(model_id_used == model_id for model_id_used, _prompt, _tokens in model_requests)
    assert all(stage.status == ContextSummaryStageStatus.COMPLETED for stage in stages)
    assert all(stage.expected_fragment_count == stage.succeeded_fragment_count for stage in stages)
    assert all(stage.model_id == model_id for stage in stages)

    raw_stage = next(stage for stage in stages if stage.lower_stage_key is None)
    raw_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == raw_stage.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    assert raw_stage.expected_fragment_count > 1
    assert len(raw_fragments) == raw_stage.expected_fragment_count == raw_stage.succeeded_fragment_count
    assert [fragment.fragment_index for fragment in raw_fragments] == list(range(raw_stage.expected_fragment_count))
    assert all(fragment.status.value == "completed" for fragment in raw_fragments)
    assert raw_fragments[0].message_start_id == 1
    assert raw_fragments[-1].message_end_id == target_message_id
    assert all(1 <= fragment.message_start_id <= fragment.message_end_id <= target_message_id for fragment in raw_fragments)
    covered_message_ids = {message_id for fragment in raw_fragments for message_id in range(fragment.message_start_id, fragment.message_end_id + 1)}
    assert covered_message_ids == set(original_contents)
    assert [message.id for message in persisted_messages] == list(original_contents)
    assert {message.id: message.content for message in persisted_messages} == original_contents

    chunk_parts: dict[int, list[tuple[int, str, str]]] = {}
    complete_messages: list[dict] = []
    for _model_id, prompt, _prompt_tokens in model_requests:
        for message in json.loads(prompt).get("message_chunks", []):
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("message_id"), int) and isinstance(message.get("part"), int) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str):
                chunk_parts.setdefault(message["message_id"], []).append((message["part"], message["role"], message["content"]))
            elif message.get("role") in {MessageRole.USER.value, MessageRole.ASSISTANT.value} and isinstance(message.get("content"), str):
                complete_messages.append(message)

    chunk_messages: list[dict] = []
    for message_id, parts in chunk_parts.items():
        ordered_parts = sorted(parts)
        assert [part for part, _role, _payload in ordered_parts] == list(range(len(ordered_parts)))
        assert len({role for _part, role, _payload in ordered_parts}) == 1
        payload = json.loads("".join(chunk for _part, _role, chunk in ordered_parts))
        assert payload["role"] == ordered_parts[0][1]
        chunk_messages.append(payload)

    observed_messages = complete_messages + chunk_messages
    expected_messages = [{"role": message.role.value, "content": message.content} for message in message_rows]
    assert len(observed_messages) == len(expected_messages)
    assert sorted((message["role"], message["content"]) for message in observed_messages) == sorted((message["role"], message["content"]) for message in expected_messages)
