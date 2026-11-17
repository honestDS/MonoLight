import asyncio
import json
from contextlib import aclosing
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall
from tests.integration.subagents.subagent_board_workflow_support import (
    _create_case,
    _next_done_event,
    _notification_snapshot,
    _submit,
)


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("stream_delivery", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("with_send_message", [False, True], ids=["original-parent", "send-message-parent"])
@pytest.mark.parametrize("recover_before_release", [False, True], ids=["release", "recover-expired"])
@pytest.mark.asyncio
async def test_board_review_final_text_yields_to_user_and_resumes_without_extra_model_call(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    stream_delivery: bool,
    with_send_message: bool,
    recover_before_release: bool,
) -> None:
    from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

    case_suffix = f"{protocol}-{stream_delivery}-{with_send_message}-{recover_before_release}"
    child_final = f"inspection-board-yield-child-final-{case_suffix}"
    parent_final = f"inspection-board-yield-parent-final-{case_suffix}"
    user_message = f"inspection-board-yield-user-message-{case_suffix}"
    second_user_message = f"inspection-board-yield-second-user-message-{case_suffix}"
    user_final = f"inspection-board-yield-user-final-{case_suffix}"
    user_request_id = f"inspection-board-yield-user-request-{case_suffix}"
    second_user_request_id = f"inspection-board-yield-second-user-request-{case_suffix}"
    send_request_id = f"inspection-board-yield-send-request-{case_suffix}"
    send_message_instruction = f"inspection-board-yield-send-instruction-{case_suffix}"
    release_parent_request = asyncio.Event()
    parent_request_started = asyncio.Event()
    parent_task: asyncio.Task | None = None

    async with _create_case(image_chat_runtime, protocol=protocol, stream_delivery=stream_delivery) as case:
        runtime = case.runtime
        child = case.children[0]
        review_parent_work_id = case.origin_work_id
        review_parent_request_id = "inspection-origin-request"

        if with_send_message:
            review_parent_work_id = await _submit(
                runtime,
                request_id=send_request_id,
                message=f"{send_message_instruction} and report the result.",
                expected_status="accepted",
            )
            review_parent_request_id = send_request_id
            assert review_parent_work_id != case.origin_work_id
            async with runtime.factory() as db:
                new_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                assert new_parent is not None
                execution_state = deepcopy(new_parent.execution_state)
                execution_state["stream_requested"] = stream_delivery
                new_parent.execution_state = execution_state
                await db.commit()

            send_arguments = {
                "operation": "send_message",
                "subagent_id": child["subagent_id"],
                "message": send_message_instruction,
            }
            send_call = InternalToolCall(
                id=f"inspection-board-yield-send-call-{case_suffix}",
                name=AGENT_WORK_TOOL_NAME,
                arguments=send_arguments,
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]))
            send_waiting = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert send_waiting is not None
            assert send_waiting.id == review_parent_work_id
            assert send_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert not runtime.outputs

            async with runtime.factory() as db:
                old_parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                send_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == review_parent_work_id))).scalars().one()
                assert old_parent is not None
                assert send_call_row.child_work_id == child["work_id"]

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
        child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
        assert child_done is not None
        assert child_done.id == child["work_id"]
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            review_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
            assert review_parent is not None
            assert review_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert review_parent.sequence_no is not None
            review_parent_sequence_before = review_parent.sequence_no
            review_parent_attempt_count = review_parent.attempt_count

        transformer = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        fixture_post_json = transformer._post_json

        async def hold_parent_post_json(_transformer, **kwargs):
            response = await fixture_post_json(_transformer, **kwargs)
            assert not parent_request_started.is_set()
            parent_request_started.set()
            await release_parent_request.wait()
            return response

        original_yield_board_review = session_reply_work_item_crud.yield_board_review
        yield_board_review_patched = False
        release_interrupted = False
        if recover_before_release:

            async def interrupt_yield_board_review(db, *, work_id, worker_id, expired_at=None, commit=True):
                nonlocal release_interrupted
                if not release_interrupted and expired_at is None and work_id == review_parent_work_id:
                    release_interrupted = True
                    raise asyncio.CancelledError
                return await original_yield_board_review(
                    db,
                    work_id=work_id,
                    worker_id=worker_id,
                    expired_at=expired_at,
                    commit=commit,
                )

            monkeypatch.setattr(session_reply_work_item_crud, "yield_board_review", interrupt_yield_board_review)
            yield_board_review_patched = True
        monkeypatch.setattr(transformer, "_post_json", hold_parent_post_json)
        consumed_events: list[dict] = []
        delivered_event_sequence_by_work_id: dict[int, int] = {}
        reconnected_stream = None
        user_stream_task: asyncio.Task | None = None
        try:
            async with aclosing(
                session_reply_queue_manager.wait_for_session_stream(
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    after_work_sequence_no=review_parent_sequence_before - 1,
                    interactive_only=True,
                    delivered_event_sequence_by_work_id=delivered_event_sequence_by_work_id,
                )
            ) as session_stream:
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
                parent_provider_start = len(runtime.provider_payloads)
                parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
                await asyncio.wait_for(parent_request_started.wait(), timeout=5)
                if stream_delivery:
                    initial_stream_event = await asyncio.wait_for(anext(session_stream), timeout=5)
                    consumed_events.append(initial_stream_event)
                    assert initial_stream_event.get("work_id") == review_parent_work_id

                user_work_id = await asyncio.wait_for(
                    _submit(
                        runtime,
                        request_id=user_request_id,
                        message=user_message,
                        expected_status="queued",
                    ),
                    timeout=5,
                )
                assert user_work_id != review_parent_work_id
                assert await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5) is None

                async with runtime.factory() as db:
                    running_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                    queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                    assert running_parent is not None and queued_user is not None
                    assert running_parent.status == SessionReplyWorkStatus.RUNNING
                    assert running_parent.locked_by is not None
                    assert queued_user.status == SessionReplyWorkStatus.READY_FOR_LLM

                release_parent_request.set()
                if recover_before_release:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(parent_task, timeout=5)
                    parent_task = None
                    assert release_interrupted
                    monkeypatch.setattr(session_reply_work_item_crud, "yield_board_review", original_yield_board_review)

                    async with runtime.factory() as db:
                        interrupted_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                        assert interrupted_parent is not None
                        assert interrupted_parent.status == SessionReplyWorkStatus.RUNNING, f"work.error={interrupted_parent.error!r}"
                        old_worker_id = interrupted_parent.locked_by
                        assert old_worker_id is not None, f"work.error={interrupted_parent.error!r}"
                        old_sequence_no = interrupted_parent.sequence_no
                        assert old_sequence_no is not None, f"work.error={interrupted_parent.error!r}"
                        interrupted_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, review_parent_work_id)
                        assert isinstance(interrupted_checkpoint, dict), f"work.error={interrupted_parent.error!r}"
                        assert interrupted_checkpoint.get("yield_to_user_pending") is True, f"work.error={interrupted_parent.error!r}"
                        interrupted_pending_generation = interrupted_checkpoint.get("pending_generation")
                        assert isinstance(interrupted_pending_generation, dict), f"work.error={interrupted_parent.error!r}"
                        interrupted_pending_result = interrupted_pending_generation.get("result")
                        assert isinstance(interrupted_pending_result, dict), f"work.error={interrupted_parent.error!r}"
                        interrupted_pending_message = interrupted_pending_result.get("message")
                        assert isinstance(interrupted_pending_message, dict), f"work.error={interrupted_parent.error!r}"
                        assert interrupted_pending_message.get("content") == parent_final, f"work.error={interrupted_parent.error!r}"

                        interrupted_parent.lock_until = 0
                        await db.commit()
                        recovered_count, terminal_claims = await session_reply_work_item_crud.recover_expired(db)
                        assert recovered_count == 1, f"work.error={interrupted_parent.error!r}; terminal_claims={terminal_claims!r}"
                        assert terminal_claims == [], f"work.error={interrupted_parent.error!r}"

                        yielded_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                        assert yielded_parent is not None
                        await db.refresh(yielded_parent)
                        assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM, f"work.error={yielded_parent.error!r}"
                        assert yielded_parent.attempt_count == review_parent_attempt_count, f"work.error={yielded_parent.error!r}"
                        assert yielded_parent.sequence_no is not None and yielded_parent.sequence_no != old_sequence_no, f"work.error={yielded_parent.error!r}"
                        assert yielded_parent.locked_by is None and yielded_parent.lock_until is None, f"work.error={yielded_parent.error!r}"
                        recovered_parent_state = (
                            yielded_parent.status,
                            yielded_parent.sequence_no,
                            yielded_parent.locked_by,
                            yielded_parent.lock_until,
                        )
                        assert await original_yield_board_review(db, work_id=review_parent_work_id, worker_id=old_worker_id) is False, f"work.error={yielded_parent.error!r}"
                        await db.refresh(yielded_parent)
                        assert (
                            yielded_parent.status,
                            yielded_parent.sequence_no,
                            yielded_parent.locked_by,
                            yielded_parent.lock_until,
                        ) == recovered_parent_state, f"work.error={yielded_parent.error!r}"
                else:
                    yielded_parent = await asyncio.wait_for(parent_task, timeout=5)
                    parent_task = None
                    assert yielded_parent is not None
                    assert yielded_parent.id == review_parent_work_id
                    assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM, f"work.error={yielded_parent.error!r}"
                assert len(runtime.provider_payloads) == parent_provider_start + 1
                monkeypatch.setattr(transformer, "_post_json", fixture_post_json)

                async with runtime.factory() as db:
                    review_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                    queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                    assert review_parent is not None and queued_user is not None
                    assert review_parent.status == SessionReplyWorkStatus.READY_FOR_LLM, f"work.error={review_parent.error!r}"
                    assert review_parent.result_message_id is None
                    assert review_parent.locked_by is None
                    assert review_parent.lock_until is None
                    assert review_parent.sequence_no is not None
                    assert queued_user.sequence_no is not None
                    assert review_parent.sequence_no > queued_user.sequence_no
                    assert review_parent.attempt_count == review_parent_attempt_count

                    checkpoint = await session_reply_work_item_crud.get_checkpoint(db, review_parent_work_id)
                    assert isinstance(checkpoint, dict)
                    assert checkpoint.get("yield_to_user_pending") is True, f"work.error={review_parent.error!r}"
                    pending_generation = checkpoint.get("pending_generation")
                    assert isinstance(pending_generation, dict)
                    pending_result = pending_generation.get("result")
                    assert isinstance(pending_result, dict)
                    pending_message = pending_result.get("message")
                    assert isinstance(pending_message, dict)
                    assert pending_message.get("content") == parent_final
                    assert user_message not in json.dumps(checkpoint, ensure_ascii=False)
                    pending_generation_before_second = deepcopy(pending_generation)

                    parent_messages = (
                        (
                            await db.execute(
                                select(Message).where(
                                    Message.session_id == runtime.session_id,
                                    Message.uid == runtime.uid,
                                    Message.profile_id == runtime.profile.id,
                                    Message.role == MessageRole.ASSISTANT,
                                    Message.type == MessageType.TEXT,
                                    Message.content == parent_final,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert parent_messages == []

                    _, session_events = await _notification_snapshot(db, session_id=runtime.session_id)
                    assert not any(event[2].get("type") == "done" and event[2].get("work_id") == review_parent_work_id for event in session_events)
                    parent_stream_rows = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == review_parent_work_id))).scalars().all()
                    assert not any(row.event.get("type") == "done" and row.work_id == review_parent_work_id for row in parent_stream_rows)

                if stream_delivery:
                    await session_stream.aclose()

                second_user_work_id = await asyncio.wait_for(
                    _submit(
                        runtime,
                        request_id=second_user_request_id,
                        message=second_user_message,
                        expected_status="queued",
                    ),
                    timeout=5,
                )
                assert second_user_work_id not in {review_parent_work_id, user_work_id}
                async with runtime.factory() as db:
                    review_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                    first_user_work = await db.get(SessionReplyWorkItem, user_work_id)
                    second_user_work = await db.get(SessionReplyWorkItem, second_user_work_id)
                    assert review_parent is not None and first_user_work is not None and second_user_work is not None
                    assert review_parent.status == SessionReplyWorkStatus.READY_FOR_LLM, f"work.error={review_parent.error!r}"
                    assert review_parent.sequence_no is not None
                    assert first_user_work.sequence_no is not None
                    assert second_user_work.sequence_no is not None
                    assert review_parent.sequence_no > first_user_work.sequence_no, f"work.error={review_parent.error!r}"
                    assert review_parent.sequence_no > second_user_work.sequence_no, f"work.error={review_parent.error!r}"
                    second_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, review_parent_work_id)
                    assert isinstance(second_checkpoint, dict), f"work.error={review_parent.error!r}"
                    assert second_checkpoint.get("pending_generation") == pending_generation_before_second, f"work.error={review_parent.error!r}"
                    second_checkpoint_dump = json.dumps(second_checkpoint, ensure_ascii=False)
                    assert user_message not in second_checkpoint_dump
                    assert second_user_message not in second_checkpoint_dump

                if stream_delivery:
                    reconnected_stream = session_reply_queue_manager.wait_for_session_stream(
                        uid=runtime.uid,
                        session_id=runtime.session_id,
                        after_work_sequence_no=review_parent_sequence_before,
                        interactive_only=True,
                        delivered_event_sequence_by_work_id=delivered_event_sequence_by_work_id,
                    )
                    user_stream_task = asyncio.create_task(
                        _next_done_event(
                            reconnected_stream,
                            work_id=user_work_id,
                            content=user_final,
                            events=consumed_events,
                        )
                    )

                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
                user_provider_start = len(runtime.provider_payloads)
                user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
                assert user_done is not None
                assert user_done.id == user_work_id
                assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
                user_provider_payloads = runtime.provider_payloads[user_provider_start:]
                assert len(user_provider_payloads) == 1
                user_provider_dump = json.dumps(user_provider_payloads, ensure_ascii=False)
                assert user_message in user_provider_dump
                assert second_user_message in user_provider_dump
                assert not runtime.outputs

                user_status = await asyncio.wait_for(runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}"), timeout=5)
                assert user_status.status_code == 200
                user_data = user_status.json()["data"]
                assert user_data["request_ids"] == [user_request_id, second_user_request_id]
                assert user_data["response"]["choices"][0]["message"]["content"] == user_final

                async with runtime.factory() as db:
                    review_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                    user_work = await db.get(SessionReplyWorkItem, user_work_id)
                    second_user_work = await db.get(SessionReplyWorkItem, second_user_work_id)
                    assert review_parent is not None and user_work is not None and second_user_work is not None
                    assert review_parent.status == SessionReplyWorkStatus.READY_FOR_LLM, f"work.error={review_parent.error!r}"
                    assert second_user_work.status == SessionReplyWorkStatus.MERGED
                    assert second_user_work.merged_into_id == user_work_id
                    assert user_work.input_message_ids is not None
                    assert len(user_work.input_message_ids) == 2
                    input_messages = [await db.get(Message, message_id) for message_id in user_work.input_message_ids]
                    assert all(message is not None for message in input_messages)
                    assert [message.content for message in input_messages if message is not None] == [user_message, second_user_message]
                    checkpoint = await session_reply_work_item_crud.get_checkpoint(db, review_parent_work_id)
                    assert isinstance(checkpoint, dict)
                    assert user_message not in json.dumps(checkpoint, ensure_ascii=False)
                    assert second_user_message not in json.dumps(checkpoint, ensure_ascii=False)

                provider_count = len(runtime.provider_payloads)
                assert not runtime.outputs
                parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
                assert parent_done is not None
                assert parent_done.id == review_parent_work_id
                assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED, f"work.error={parent_done.error!r}"
                assert len(runtime.provider_payloads) == provider_count
                assert not runtime.outputs

                parent_status = await asyncio.wait_for(runtime.client.get(f"/api/v1/chat/reply-works/{review_parent_work_id}"), timeout=5)
                assert parent_status.status_code == 200
                parent_data = parent_status.json()["data"]
                assert parent_data["request_ids"] == [review_parent_request_id]
                assert user_request_id not in parent_data["request_ids"]
                assert parent_data["response"]["choices"][0]["message"]["content"] == parent_final

                async with runtime.factory() as db:
                    review_parent = await db.get(SessionReplyWorkItem, review_parent_work_id)
                    assert review_parent is not None
                    assert review_parent.status == SessionReplyWorkStatus.SUCCEEDED, f"work.error={review_parent.error!r}"
                    assert review_parent.result_message_id == parent_data["result_message_id"]
                    final_messages = (
                        (
                            await db.execute(
                                select(Message).where(
                                    Message.session_id == runtime.session_id,
                                    Message.uid == runtime.uid,
                                    Message.profile_id == runtime.profile.id,
                                    Message.role == MessageRole.ASSISTANT,
                                    Message.type == MessageType.TEXT,
                                    Message.content == parent_final,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert len(final_messages) == 1

                if stream_delivery:
                    assert user_stream_task is not None
                    await asyncio.wait_for(user_stream_task, timeout=5)
                    user_stream_task = None
                    assert reconnected_stream is not None
                    await _next_done_event(reconnected_stream, work_id=review_parent_work_id, content=parent_final, events=consumed_events)
                    with pytest.raises(StopAsyncIteration):
                        await asyncio.wait_for(anext(reconnected_stream), timeout=5)
                else:
                    await _next_done_event(session_stream, work_id=user_work_id, content=user_final, events=consumed_events)
                    await _next_done_event(session_stream, work_id=review_parent_work_id, content=parent_final, events=consumed_events)
                    with pytest.raises(StopAsyncIteration):
                        await asyncio.wait_for(anext(session_stream), timeout=5)

                done_events = [event for event in consumed_events if event.get("type") == "done"]
                assert len(done_events) == 2
                assert [event.get("work_id") for event in done_events] == [user_work_id, review_parent_work_id]
                assert len({event.get("work_id") for event in done_events}) == 2
                assert [event.get("request_ids") for event in done_events] == [[user_request_id, second_user_request_id], [review_parent_request_id]]
                done_work_sequences = [event.get("work_sequence_no") for event in done_events]
                assert all(type(sequence_no) is int for sequence_no in done_work_sequences)
                assert done_work_sequences == sorted(done_work_sequences)
                for work_id in (user_work_id, review_parent_work_id):
                    event_sequences = [event["event_sequence_no"] for event in consumed_events if event.get("work_id") == work_id and type(event.get("event_sequence_no")) is int]
                    assert event_sequences == sorted(event_sequences)
                    assert len(event_sequences) == len(set(event_sequences))
        finally:
            release_parent_request.set()
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)
            if yield_board_review_patched:
                monkeypatch.setattr(session_reply_work_item_crud, "yield_board_review", original_yield_board_review)
            if user_stream_task is not None:
                if not user_stream_task.done():
                    user_stream_task.cancel()
                await asyncio.gather(user_stream_task, return_exceptions=True)
            if reconnected_stream is not None:
                await reconnected_stream.aclose()
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.gather(parent_task, return_exceptions=True)


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("stream_delivery", [False, True], ids=["non-stream", "stream"])
@pytest.mark.asyncio
async def test_subagent_board_review_yields_after_parallel_tools_and_resumes_without_replay(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
    stream_delivery: bool,
) -> None:
    from app.core.constants import FILE_TOOL_NAME
    from app.core.tools.file_tool import FileToolExecutor

    case_suffix = f"{protocol}-{stream_delivery}"
    child_final = f"parallel-tools-child-final-{case_suffix}"
    user_message = f"parallel-tools-user-message-{case_suffix}"
    user_final = f"parallel-tools-user-final-{case_suffix}"
    parent_final = f"parallel-tools-parent-final-{case_suffix}"
    user_request_id = f"parallel-tools-user-request-{case_suffix}"
    slow_path = (tmp_path / f"parallel-tools-slow-{case_suffix}.txt").resolve()
    fast_path = (tmp_path / f"parallel-tools-fast-{case_suffix}.txt").resolve()
    slow_content = f"slow-file-content-{case_suffix}-unique"
    fast_content = f"fast-file-content-{case_suffix}-unique"
    slow_call = InternalToolCall(
        id=f"parallel-tools-slow-call-{case_suffix}",
        name=FILE_TOOL_NAME,
        arguments={"operation": "write", "path": str(slow_path), "content": slow_content},
    )
    fast_call = InternalToolCall(
        id=f"parallel-tools-fast-call-{case_suffix}",
        name=FILE_TOOL_NAME,
        arguments={"operation": "write", "path": str(fast_path), "content": fast_content},
    )
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_finished = asyncio.Event()
    slow_cancelled = asyncio.Event()
    execute_paths: list[str] = []
    parent_task: asyncio.Task | None = None

    async with _create_case(image_chat_runtime, protocol=protocol, stream_delivery=stream_delivery) as case:
        runtime = case.runtime
        child = case.children[0]
        try:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM

            original_execute = FileToolExecutor.execute

            async def gated_execute(executor, *args, **kwargs):
                path = kwargs.get("path")
                if path is None and len(args) >= 2:
                    path = args[1]
                execute_paths.append(str(path))
                if path == str(slow_path):
                    slow_started.set()
                    try:
                        await release_slow.wait()
                        return await original_execute(executor, *args, **kwargs)
                    except asyncio.CancelledError:
                        slow_cancelled.set()
                        raise

                result = await original_execute(executor, *args, **kwargs)
                if path == str(fast_path):
                    fast_finished.set()
                return result

            monkeypatch.setattr(FileToolExecutor, "execute", gated_execute)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[slow_call, fast_call]))
            parent_provider_start = len(runtime.provider_payloads)
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))

            await asyncio.wait_for(slow_started.wait(), timeout=5)
            await asyncio.wait_for(fast_finished.wait(), timeout=5)
            assert not slow_cancelled.is_set()

            user_work_id = await asyncio.wait_for(
                _submit(
                    runtime,
                    request_id=user_request_id,
                    message=user_message,
                    expected_status="queued",
                ),
                timeout=5,
            )
            assert user_work_id != case.origin_work_id

            provider_count_before_run_next = len(runtime.provider_payloads)
            assert provider_count_before_run_next == parent_provider_start + 1
            assert await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5) is None
            assert len(runtime.provider_payloads) == provider_count_before_run_next
            assert not runtime.outputs

            async with runtime.factory() as db:
                running_parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert running_parent is not None and queued_user is not None
                assert running_parent.status == SessionReplyWorkStatus.RUNNING
                assert running_parent.locked_by is not None
                assert running_parent.result_message_id is None
                assert queued_user.status == SessionReplyWorkStatus.READY_FOR_LLM

            release_slow.set()
            yielded_parent = await asyncio.wait_for(parent_task, timeout=5)
            parent_task = None
            assert yielded_parent is not None
            assert yielded_parent.id == case.origin_work_id
            assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert yielded_parent.locked_by is None
            assert yielded_parent.result_message_id is None
            assert yielded_parent.sequence_no is not None
            assert execute_paths.count(str(slow_path)) == 1
            assert execute_paths.count(str(fast_path)) == 1
            assert slow_path.read_text(encoding="utf-8") == slow_content
            assert fast_path.read_text(encoding="utf-8") == fast_content

            async with runtime.factory() as db:
                yielded_parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert yielded_parent is not None and queued_user is not None
                assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert yielded_parent.locked_by is None
                assert yielded_parent.result_message_id is None
                assert yielded_parent.sequence_no is not None
                assert queued_user.sequence_no is not None
                assert yielded_parent.sequence_no > queued_user.sequence_no

                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert "pending_generation" not in checkpoint
                checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
                write_calls = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.name == FILE_TOOL_NAME and tool_call.arguments.get("operation") == "write" and tool_call.arguments.get("path") in {str(slow_path), str(fast_path)}]
                assert len(write_calls) == 2
                assert len({tool_call.id for tool_call in write_calls}) == 2
                assert {(tool_call.arguments.get("path"), tool_call.arguments.get("content")) for tool_call in write_calls} == {
                    (str(slow_path), slow_content),
                    (str(fast_path), fast_content),
                }
                write_call_ids = {tool_call.id for tool_call in write_calls}
                checkpoint_results = [message for message in checkpoint_messages if message.role == MessageRole.TOOL and message.tool_call_id in write_call_ids]
                assert len(checkpoint_results) == 2
                assert {message.tool_call_id for message in checkpoint_results} == write_call_ids
                assert all(get_tool_execution_status(message) == "succeeded" for message in checkpoint_results)
                assert all(json.loads(message.content or "{}").get("status") == "success" for message in checkpoint_results)
                assert "execution_unknown" not in json.dumps(checkpoint, ensure_ascii=False)

            assert not runtime.outputs
            user_provider_start = len(runtime.provider_payloads)
            assert len(runtime.provider_payloads[parent_provider_start:user_provider_start]) == 1
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[user_provider_start:]) == 1
            assert user_message in json.dumps(runtime.provider_payloads[user_provider_start:], ensure_ascii=False)
            assert not runtime.outputs

            user_status = await asyncio.wait_for(runtime.client.get(f"/api/v1/chat/reply-works/{user_work_id}"), timeout=5)
            assert user_status.status_code == 200
            user_data = user_status.json()["data"]
            assert user_data["request_ids"] == [user_request_id]
            assert user_data["response"]["choices"][0]["message"]["content"] == user_final

            parent_provider_resume_start = len(runtime.provider_payloads)
            assert parent_provider_resume_start == user_provider_start + 1
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[parent_provider_resume_start:]) == 1
            assert not runtime.outputs
            assert slow_path.read_text(encoding="utf-8") == slow_content
            assert fast_path.read_text(encoding="utf-8") == fast_content

            async with runtime.factory() as db:
                stored_messages = (
                    (
                        await db.execute(
                            select(Message)
                            .where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                                Message.type.in_([MessageType.TOOL_CALL, MessageType.TOOL_RESULT]),
                            )
                            .order_by(Message.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                persisted_calls = []
                persisted_results = []
                for row in stored_messages:
                    message = InternalMessage.model_validate_json(row.content or "{}")
                    if message.role == MessageRole.ASSISTANT:
                        persisted_calls.extend(call for call in message.tool_calls or [] if call.id in write_call_ids)
                    elif message.role == MessageRole.TOOL and message.tool_call_id in write_call_ids:
                        persisted_results.append(message)
                assert len(persisted_calls) == 2
                assert {call.id for call in persisted_calls} == write_call_ids
                assert all([call.id for call in persisted_calls].count(call_id) == 1 for call_id in write_call_ids)
                assert len(persisted_results) == 2
                assert {message.tool_call_id for message in persisted_results} == write_call_ids
                assert all([message.tool_call_id for message in persisted_results].count(call_id) == 1 for call_id in write_call_ids)
                assert all(get_tool_execution_status(message) == "succeeded" for message in persisted_results)
                assert all(json.loads(message.content or "{}").get("status") == "success" for message in persisted_results)

                parent_status = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent_status is not None
                assert parent_status.status == SessionReplyWorkStatus.SUCCEEDED

            parent_response = await asyncio.wait_for(
                runtime.client.get(f"/api/v1/chat/reply-works/{case.origin_work_id}"),
                timeout=5,
            )
            assert parent_response.status_code == 200
            parent_data = parent_response.json()["data"]
            assert parent_data["request_ids"] == ["inspection-origin-request"]
            assert user_request_id not in parent_data["request_ids"]
            assert parent_data["response"]["choices"][0]["message"]["content"] == parent_final
        finally:
            release_slow.set()
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.wait_for(asyncio.gather(parent_task, return_exceptions=True), timeout=5)


@pytest.mark.asyncio
async def test_subagent_board_review_resume_clears_yield_marker_before_audit_and_preserves_expired_claim(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module
    from app.core.constants import FILE_TOOL_NAME
    from app.transformers.openai import OpenAIResponsesTransformer

    child_final = "audit-resume-child-final"
    user_message = "audit-resume-user-message"
    user_final = "audit-resume-user-final"
    parent_final = "audit-resume-parent-final"
    user_request_id = "audit-resume-user-request"
    write_path = (tmp_path / "audit-resume-write.txt").resolve()
    write_content = "audit-resume-file-content"
    write_call = InternalToolCall(
        id="audit-resume-write-call",
        name=FILE_TOOL_NAME,
        arguments={"operation": "write", "path": str(write_path), "content": write_content},
    )
    release_parent_request = asyncio.Event()
    parent_request_started = asyncio.Event()
    parent_task: asyncio.Task | None = None
    observed_audits: list[int] = []
    user_completed = False

    async with _create_case(image_chat_runtime, protocol="OPENAI_RESPONSES", stream_delivery=True) as case:
        runtime = case.runtime
        child = case.children[0]
        transformer = OpenAIResponsesTransformer
        fixture_post_json = transformer._post_json

        try:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_final))
            child_done = await asyncio.wait_for(runtime.run_next(independent_only=True), timeout=5)
            assert child_done is not None
            assert child_done.id == child["work_id"]
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            async def hold_parent_post_json(_transformer, **kwargs):
                response = await fixture_post_json(_transformer, **kwargs)
                if not parent_request_started.is_set():
                    parent_request_started.set()
                    await release_parent_request.wait()
                return response

            monkeypatch.setattr(transformer, "_post_json", hold_parent_post_json)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[write_call]))
            parent_provider_start = len(runtime.provider_payloads)
            parent_task = asyncio.create_task(runtime.run_next(independent_only=False))
            await asyncio.wait_for(parent_request_started.wait(), timeout=5)

            user_work_id = await asyncio.wait_for(
                _submit(
                    runtime,
                    request_id=user_request_id,
                    message=user_message,
                    expected_status="queued",
                ),
                timeout=5,
            )
            assert user_work_id != case.origin_work_id
            assert await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5) is None

            async with runtime.factory() as db:
                running_parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                queued_user = await db.get(SessionReplyWorkItem, user_work_id)
                assert running_parent is not None and queued_user is not None
                assert running_parent.status == SessionReplyWorkStatus.RUNNING
                assert running_parent.locked_by is not None
                assert queued_user.status == SessionReplyWorkStatus.READY_FOR_LLM

            release_parent_request.set()
            yielded_parent = await asyncio.wait_for(parent_task, timeout=5)
            parent_task = None
            assert yielded_parent is not None
            assert yielded_parent.id == case.origin_work_id
            assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert not runtime.outputs
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)

            async with runtime.factory() as db:
                yielded_parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert yielded_parent is not None
                assert yielded_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert checkpoint.get("yield_to_user_pending") is True
                pending_generation = checkpoint.get("pending_generation")
                assert isinstance(pending_generation, dict)
                pending_result = pending_generation.get("result")
                assert isinstance(pending_result, dict)
                pending_message = pending_result.get("message")
                assert isinstance(pending_message, dict)
                pending_tool_calls = pending_message.get("tool_calls")
                assert isinstance(pending_tool_calls, list)
                assert any(isinstance(tool_call, dict) and tool_call.get("name") == write_call.name and tool_call.get("arguments") == write_call.arguments for tool_call in pending_tool_calls)

            user_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=user_final))
            user_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert user_done is not None
            assert user_done.id == user_work_id
            assert user_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[user_provider_start:]) == 1
            assert user_message in json.dumps(runtime.provider_payloads[user_provider_start:], ensure_ascii=False)
            assert not runtime.outputs
            user_completed = True

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, case.origin_work_id)
                assert parent is not None
                assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                checkpoint = await session_reply_work_item_crud.get_checkpoint(db, case.origin_work_id)
                assert isinstance(checkpoint, dict)
                assert checkpoint.get("yield_to_user_pending") is True
                assert isinstance(checkpoint.get("pending_generation"), dict)

            original_audit_tool_round = interactive_tools_module.audit_tool_round

            async def observe_audit(db, **kwargs):
                claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
                tool_calls = kwargs.get("tool_calls")
                target_call = isinstance(claim, (tuple, list)) and len(claim) == 2 and claim[0] == case.origin_work_id and isinstance(tool_calls, list) and any(isinstance(tool_call, InternalToolCall) and tool_call.name == write_call.name and tool_call.arguments == write_call.arguments for tool_call in tool_calls)
                if not target_call:
                    return await original_audit_tool_round(db, **kwargs)

                assert user_completed
                assert not observed_audits
                observed_audits.append(claim[0])
                async with runtime.factory() as recovery_db:
                    parent = await recovery_db.get(SessionReplyWorkItem, case.origin_work_id)
                    assert parent is not None
                    assert parent.status == SessionReplyWorkStatus.RUNNING
                    assert parent.execution_state.get("subagent_board_review_pending") is True
                    checkpoint = await session_reply_work_item_crud.get_checkpoint(recovery_db, case.origin_work_id)
                    assert isinstance(checkpoint, dict)
                    assert isinstance(checkpoint.get("pending_generation"), dict)
                    assert checkpoint.get("yield_to_user_pending") is not True
                    stream_events = (await recovery_db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == case.origin_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
                    assert any(event.event.get("type") != "work_suspended" for event in stream_events)

                    old_locked_by = parent.locked_by
                    old_lock_until = parent.lock_until
                    old_sequence_no = parent.sequence_no
                    old_attempt_count = parent.attempt_count
                    assert isinstance(old_locked_by, str) and old_locked_by
                    assert isinstance(old_lock_until, int)
                    assert isinstance(old_sequence_no, int)

                    parent.lock_until = 0
                    await recovery_db.commit()
                    try:
                        recovered_count, terminal_claims = await session_reply_work_item_crud.recover_expired(recovery_db)
                        assert recovered_count == 1
                        assert terminal_claims == [(case.origin_work_id, old_locked_by, "Stream interrupted after partial response")]
                        await recovery_db.refresh(parent)
                        assert parent.status == SessionReplyWorkStatus.RUNNING
                        assert parent.locked_by == old_locked_by
                        assert parent.lock_until == 0
                        assert parent.sequence_no == old_sequence_no
                        assert parent.attempt_count == old_attempt_count
                    finally:
                        await recovery_db.rollback()
                        restored_parent = await recovery_db.get(SessionReplyWorkItem, case.origin_work_id)
                        assert restored_parent is not None
                        restored_parent.lock_until = old_lock_until
                        await recovery_db.commit()

                return await original_audit_tool_round(db, **kwargs)

            monkeypatch.setattr(interactive_tools_module, "audit_tool_round", observe_audit)
            parent_resume_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await asyncio.wait_for(runtime.run_next(independent_only=False), timeout=5)
            assert parent_done is not None
            assert parent_done.id == case.origin_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads[parent_resume_provider_start:]) == 1
            assert parent_resume_provider_start == user_provider_start + 1
            assert len(runtime.provider_payloads[parent_provider_start:]) == 3
            assert not runtime.outputs
            assert len(observed_audits) == 1
            assert write_path.read_text(encoding="utf-8") == write_content
        finally:
            release_parent_request.set()
            monkeypatch.setattr(transformer, "_post_json", fixture_post_json)
            if parent_task is not None:
                if not parent_task.done():
                    parent_task.cancel()
                await asyncio.gather(parent_task, return_exceptions=True)
