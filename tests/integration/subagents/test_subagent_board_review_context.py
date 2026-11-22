import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import END_SESSION_TOOL_NAME
from app.core.crud.session.session import session_crud
from app.core.exceptions import LLMContextLengthException
from app.core.utils.context_summary import cleanup as context_summary_cleanup
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import service as context_summary_service
from app.core.utils.context_summary import stage as context_summary_stage
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
    StoredConversationSummary,
)
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall
from app.transformers.openai import OpenAIResponsesTransformer
from tests.integration.subagents.subagent_board_workflow_support import _create_case, _submit


def _provider_subagent_resume_results(payload: dict) -> list[dict]:
    items = payload.get("input")
    if not isinstance(items, list):
        items = payload.get("messages")
    if not isinstance(items, list):
        return []

    results = []
    for item in items:
        if not isinstance(item, dict) or item.get("role") != MessageRole.USER.value:
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        else:
            continue

        for text in texts:
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("user_message"), str):
                try:
                    parsed = json.loads(parsed["user_message"])
                except (TypeError, json.JSONDecodeError):
                    continue
            if not isinstance(parsed, dict) or parsed.get("type") != "external_work_resume" or parsed.get("source") != "subagent":
                continue
            resume_results = parsed.get("results")
            if isinstance(resume_results, list):
                results.extend(result for result in resume_results if isinstance(result, dict))
    return results


async def _complete_queued_user_reply(runtime, *, request_id: str, question: str, answer: str) -> int:
    work_id = await _submit(runtime, request_id=request_id, message=question, expected_status="accepted")
    runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=answer))
    provider_start = len(runtime.provider_payloads)
    completed = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
    assert completed is not None
    assert completed.id == work_id
    assert completed.status == SessionReplyWorkStatus.SUCCEEDED
    assert len(runtime.provider_payloads) == provider_start + 1
    assert not runtime.outputs
    return work_id


async def _persist_summary(runtime, *, work_id: int, summary_text: str) -> int:
    async with runtime.factory() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        assert work is not None
        assert work.status == SessionReplyWorkStatus.SUCCEEDED
        assert work.result_message_id is not None
        result_message = await db.get(Message, work.result_message_id)
        assert result_message is not None
        assert result_message.role == MessageRole.ASSISTANT
        assert result_message.type == MessageType.TEXT

        session = await db.get(ChatSession, runtime.session_id)
        assert session is not None
        previous_revision = session.context_summary_revision
        stored_summary = StoredConversationSummary(conversation_summary=summary_text).model_dump_json(exclude_none=True)
        updated = await session_crud.update_context_summary(
            db,
            session_id=runtime.session_id,
            uid=runtime.uid,
            expected_message_id=session.context_summary_message_id,
            expected_revision=session.context_summary_revision,
            expected_content_revision=session.context_content_revision,
            summary=stored_summary,
            message_id=result_message.id,
        )
        assert updated is True
        await db.commit()
        await db.refresh(session)
        assert session.context_summary_revision == previous_revision + 1
        assert session.context_summary_message_id == result_message.id
        assert session.context_summary == stored_summary
        return result_message.id


@pytest.mark.parametrize("protocol", [pytest.param("OPENAI", id="openai"), pytest.param("OPENAI_RESPONSES", id="openai-responses")])
@pytest.mark.parametrize("summary_rounds", [0, 1, 2], ids=["no-summary", "one-summary", "multiple-summaries"])
@pytest.mark.parametrize("already_reported", [False, True], ids=["new-result", "already-reported"])
@pytest.mark.asyncio
async def test_board_review_resumes_with_latest_context_and_model_selected_delivery(
    image_chat_runtime,
    protocol: str,
    summary_rounds: int,
    already_reported: bool,
) -> None:
    case_id = f"{protocol.lower()}-{summary_rounds}-{already_reported}"
    child_final = f"inspection-review-context-child-final-{case_id}"
    report_marker = f"inspection-review-context-reported-marker-{case_id}"
    unrelated_marker = f"inspection-review-context-unrelated-marker-{case_id}"
    latest_question = f"inspection-review-context-latest-question-{case_id}"
    latest_answer = f"inspection-review-context-latest-answer-{case_id}"
    fresh_answer = f"I already reported the child result in an earlier reply. Explicit report marker: {report_marker}." if already_reported else f"A separate topic update: {unrelated_marker}."
    fresh_parent_final = f"Fresh parent delivery: {child_final}."

    async with _create_case(image_chat_runtime, protocol=protocol, child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert origin.result_message_id is None
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().all()
            assert len(calls) == 1
            call = calls[0]
            assert call.child_work_id == child["work_id"]
            assert call.delivered is True
            assert call.tool_result_message_id is not None
            call_snapshot = (call.id, call.parent_work_id, call.child_work_id, call.tool_call_id, call.tool_result_message_id, call.delivered)
            session = await db.get(ChatSession, runtime.session_id)
            assert session is not None
            initial_summary_revision = session.context_summary_revision

        first_work_id = await _complete_queued_user_reply(
            runtime,
            request_id=f"inspection-review-context-fresh-{case_id}",
            question=f"inspection-review-context-follow-up-question-{case_id}",
            answer=fresh_answer,
        )
        compacted_answers = [fresh_answer]
        summaries = []

        for summary_index in range(summary_rounds):
            if summary_index > 0:
                intermediate_answer = f"inspection-review-context-intermediate-answer-{summary_index}-{case_id}"
                intermediate_work_id = await _complete_queued_user_reply(
                    runtime,
                    request_id=f"inspection-review-context-intermediate-{summary_index}-{case_id}",
                    question=f"inspection-review-context-intermediate-question-{summary_index}-{case_id}",
                    answer=intermediate_answer,
                )
                compacted_answers.append(intermediate_answer)
            else:
                intermediate_work_id = first_work_id

            reported_fact = "yes" if already_reported else "no"
            if summary_index == 0:
                summary_text = f"Purpose: verify and report the original child task result. Child result already reported: {reported_fact}."
            else:
                summary_text = f"Task purpose: verify and report the original child task result. A prior reply reported it: {reported_fact}."
            if already_reported:
                summary_text += f" Report marker: {report_marker}."
            assert child_final not in summary_text
            boundary_id = await _persist_summary(runtime, work_id=intermediate_work_id, summary_text=summary_text)
            if summaries:
                assert boundary_id > summaries[-1][1]
            summaries.append((summary_text, boundary_id))

        await _complete_queued_user_reply(
            runtime,
            request_id=f"inspection-review-context-latest-{case_id}",
            question=latest_question,
            answer=latest_answer,
        )

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert origin.result_message_id is None
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().all()
            assert len(calls) == 1
            current_call = calls[0]
            assert (current_call.id, current_call.parent_work_id, current_call.child_work_id, current_call.tool_call_id, current_call.tool_result_message_id, current_call.delivered) == call_snapshot

            session = await db.get(ChatSession, runtime.session_id)
            assert session is not None
            if summary_rounds:
                assert session.context_summary is not None
                stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
                latest_summary, latest_boundary_id = summaries[-1]
                assert stored_summary.conversation_summary == latest_summary
                assert session.context_summary_message_id == latest_boundary_id
                assert session.context_summary_revision == initial_summary_revision + summary_rounds
                assert stored_summary.user_message is None
                assert child_final not in session.context_summary
                assert all(answer not in session.context_summary for answer in compacted_answers)
            else:
                assert session.context_summary is None

        resume_provider_start = len(runtime.provider_payloads)
        if already_reported:
            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id=f"inspection-review-context-end-{case_id}",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": True},
                        )
                    ],
                )
            )
        else:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=fresh_parent_final))

        resumed = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
        assert resumed is not None
        assert resumed.id == case.origin_work_id
        assert resumed.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == resume_provider_start + 1
        assert not runtime.outputs

        provider_payload = runtime.provider_payloads[resume_provider_start]
        provider_dump = json.dumps(provider_payload, ensure_ascii=False)
        assert latest_question in provider_dump
        assert latest_answer in provider_dump
        assert provider_dump.count(child_final) == 1
        resume_results = _provider_subagent_resume_results(provider_payload)
        assert len(resume_results) == 1
        assert resume_results[0] == {
            "subagent_id": child["subagent_id"],
            "work_id": child["work_id"],
            "status": "succeeded",
            "content": child_final,
        }
        if summary_rounds:
            latest_summary = summaries[-1][0]
            assert latest_summary in provider_dump
            assert all(summary not in provider_dump for summary, _ in summaries[:-1])
            assert all(answer not in provider_dump for answer in compacted_answers)
        else:
            assert fresh_answer in provider_dump
        if already_reported:
            assert report_marker in provider_dump

        status_response = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert status_response.status_code == 200
        status_data = status_response.json()["data"]
        assert status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        if already_reported:
            assert status_data["result_message_id"] is None
            assert status_data["response"]["skip_reply"] is True
        else:
            assert status_data["result_message_id"] is not None
            assert status_data["response"]["choices"][0]["message"]["content"] == fresh_parent_final

        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assert sum(item.get("content") == fresh_answer for item in history) == 1
        assert sum(item.get("content") == fresh_parent_final for item in history) == (0 if already_reported else 1)

        async with runtime.factory() as db:
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            if already_reported:
                assert origin.result_message_id is None
            else:
                assert origin.result_message_id == status_data["result_message_id"]
                parent_result = await db.get(Message, origin.result_message_id)
                assert parent_result is not None
                assert parent_result.type == MessageType.TEXT
                assert parent_result.content == fresh_parent_final
            final_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == case.origin_work_id))).scalars().all()
            assert len(final_calls) == 1
            final_call = final_calls[0]
            assert (final_call.id, final_call.parent_work_id, final_call.child_work_id, final_call.tool_call_id, final_call.tool_result_message_id, final_call.delivered) == call_snapshot


@pytest.mark.asyncio
async def test_board_review_keeps_child_notice_after_provider_overflow_summary_retry(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = "provider-overflow"
    child_final = f"inspection-review-overflow-child-final-{case_id}"
    compacted_answer = f"inspection-review-overflow-compacted-answer-{case_id}"
    compacted_summary = f"inspection-review-overflow-summary-{case_id}"
    older_question = f"inspection-review-overflow-older-question-{case_id}"
    older_answer = f"inspection-review-overflow-older-answer-{case_id}"
    recent_question = f"inspection-review-overflow-recent-question-{case_id}"
    recent_answer = f"inspection-review-overflow-recent-answer-{case_id}"
    retried_summary = f"inspection-review-overflow-retried-summary-{case_id}"
    parent_final = f"Fresh parent delivery after retry: {child_final}."

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", child_count=1) as case:
        runtime = case.runtime
        child = case.children[0]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        compacted_work_id = await _complete_queued_user_reply(
            runtime,
            request_id=f"inspection-review-overflow-compacted-{case_id}",
            question=f"inspection-review-overflow-unrelated-question-{case_id}",
            answer=compacted_answer,
        )
        assert child_final not in compacted_summary
        assert compacted_answer not in compacted_summary
        initial_summary_boundary_id = await _persist_summary(
            runtime,
            work_id=compacted_work_id,
            summary_text=compacted_summary,
        )

        await _complete_queued_user_reply(
            runtime,
            request_id=f"inspection-review-overflow-older-{case_id}",
            question=older_question,
            answer=older_answer,
        )
        await _complete_queued_user_reply(
            runtime,
            request_id=f"inspection-review-overflow-recent-{case_id}",
            question=recent_question,
            answer=recent_answer,
        )

        async with runtime.factory() as db:
            session = await db.get(ChatSession, runtime.session_id)
            assert session is not None
            initial_summary_revision = session.context_summary_revision
            assert initial_summary_revision == 1
            assert session.context_summary_message_id == initial_summary_boundary_id

        for module in (
            context_summary_cleanup,
            context_summary_merge,
            context_summary_reduction,
            context_summary_service,
            context_summary_stage,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", runtime.factory)

        summary_calls = 0

        async def generate_summary(*_args, **_kwargs):
            nonlocal summary_calls
            summary_calls += 1
            return context_summary_stage.GeneratedSummaryResult(
                content=retried_summary,
                message_count=1,
                completed_stage=SimpleNamespace(content=retried_summary, refinement_index=0),
            )

        monkeypatch.setattr(
            context_summary_service,
            "generate_snapshot_summary_result",
            generate_summary,
        )

        fixture_post_json = OpenAIResponsesTransformer._post_json
        provider_inputs: list[dict] = []

        async def capture_provider_post(_transformer, **kwargs):
            provider_inputs.append(kwargs["payload"])
            if len(provider_inputs) == 1:
                raise LLMContextLengthException(provider_message="maximum context length exceeded")
            return await fixture_post_json(_transformer, **kwargs)

        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", capture_provider_post)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))

        resumed = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
        assert resumed is not None
        assert resumed.id == case.origin_work_id
        assert resumed.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(provider_inputs) == 2
        assert summary_calls == 1
        assert not runtime.outputs

        for provider_input in provider_inputs:
            provider_dump = json.dumps(provider_input, ensure_ascii=False)
            assert provider_dump.count(child_final) == 1
            assert recent_question in provider_dump
            assert recent_answer in provider_dump
            assert compacted_answer not in provider_dump
            resume_results = _provider_subagent_resume_results(provider_input)
            assert resume_results == [
                {
                    "subagent_id": child["subagent_id"],
                    "work_id": child["work_id"],
                    "status": "succeeded",
                    "content": child_final,
                }
            ]

        initial_provider_dump = json.dumps(provider_inputs[0], ensure_ascii=False)
        retry_provider_dump = json.dumps(provider_inputs[1], ensure_ascii=False)
        assert compacted_summary in initial_provider_dump
        assert retried_summary not in initial_provider_dump
        assert retried_summary in retry_provider_dump

        async with runtime.factory() as db:
            session = await db.get(ChatSession, runtime.session_id)
            origin = await db.get(SessionReplyWorkItem, case.origin_work_id)
            assert session is not None
            assert session.context_summary is not None
            stored_summary = StoredConversationSummary.model_validate_json(session.context_summary)
            assert stored_summary.conversation_summary == retried_summary
            assert session.context_summary_revision == initial_summary_revision + 1
            assert session.context_summary_message_id is not None
            assert session.context_summary_message_id > initial_summary_boundary_id
            assert origin is not None
            assert origin.status == SessionReplyWorkStatus.SUCCEEDED
            assert origin.result_message_id is not None
            parent_result = await db.get(Message, origin.result_message_id)
            assert parent_result is not None
            assert parent_result.content == parent_final

        status_response = await runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}")
        assert status_response.status_code == 200
        status_data = status_response.json()["data"]
        assert status_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert status_data["response"]["choices"][0]["message"]["content"] == parent_final

        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assert sum(item.get("content") == parent_final for item in history) == 1
