import json
import time

import pytest

import app.providers.database as database_provider
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.terminal_watch import terminal_watch_reply_crud
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue.manager_result import SessionReplyResult
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.session_reply_queue_fixture import concurrent_session_factory as _concurrent_session_factory

concurrent_session_factory = _concurrent_session_factory


async def _seed_resume_fixture(
    session_factory,
    monkeypatch,
    *,
    session_id,
    minimal=False,
    event_snapshots=None,
):
    uid = "user-1"
    async with session_factory() as db:
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                profile_id=1,
                source="ws",
            )
        )
        messages = (
            []
            if minimal
            else [
                Message(
                    id=433,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content="fixture user input",
                    is_processed=True,
                ),
                Message(
                    id=554,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.ASSISTANT,
                    type=MessageType.TOOL_CALL,
                    content=json.dumps(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "fixture-old-call",
                                    "name": "file_tool",
                                    "arguments": {"operation": "read", "path": "fixture.txt"},
                                }
                            ],
                        }
                    ),
                    is_processed=True,
                ),
                Message(
                    id=555,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    content=json.dumps(
                        {
                            "role": "tool",
                            "tool_call_id": "fixture-old-call",
                            "content": "fixture result",
                        }
                    ),
                    is_processed=True,
                ),
                Message(
                    id=560,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content="fixture user input",
                    is_processed=True,
                ),
                Message(
                    id=695,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.ASSISTANT,
                    type=MessageType.TOOL_CALL,
                    content=json.dumps(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "fixture-visible-call",
                                    "name": "file_tool",
                                    "arguments": {"operation": "read", "path": "fixture.txt"},
                                }
                            ],
                        }
                    ),
                    is_processed=True,
                ),
                Message(
                    id=696,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.TOOL,
                    type=MessageType.TOOL_RESULT,
                    content=json.dumps(
                        {
                            "role": "tool",
                            "tool_call_id": "fixture-visible-call",
                            "content": "fixture result",
                        }
                    ),
                    is_processed=True,
                ),
                Message(
                    id=707,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content="fixture latest user input",
                    is_processed=True,
                ),
                Message(
                    id=718,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    role=MessageRole.ASSISTANT,
                    type=MessageType.TEXT,
                    content="fixture latest response",
                    is_processed=True,
                ),
            ]
        )
        works = (
            [
                SessionReplyWorkItem(
                    id=60,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="560",
                    dedupe_key=f"{session_id}-560",
                    status=SessionReplyWorkStatus.CANCELLED,
                    input_message_ids=[],
                    result_message_id=None,
                )
            ]
            if minimal
            else [
                SessionReplyWorkItem(
                    id=58,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="433",
                    dedupe_key=f"{session_id}-433",
                    status=SessionReplyWorkStatus.CANCELLED,
                    input_message_ids=[433],
                    result_message_id=None,
                ),
                SessionReplyWorkItem(
                    id=60,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    sequence_no=3,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="560",
                    dedupe_key=f"{session_id}-560",
                    status=SessionReplyWorkStatus.CANCELLED,
                    input_message_ids=[560],
                    result_message_id=None,
                ),
                SessionReplyWorkItem(
                    id=63,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    sequence_no=6,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="707",
                    dedupe_key=f"{session_id}-707",
                    status=SessionReplyWorkStatus.SUCCEEDED,
                    input_message_ids=[707],
                    result_message_id=718,
                    execution_state={
                        "response": {
                            "content": "fixture latest response",
                            "message_id": 718,
                            "response_id": "fixture-current-response",
                        }
                    },
                ),
            ]
        )
        default_event_snapshots = [
            (58, 12822, "turn_end", {"message_id": 554, "turn": 60, "response_id": "response-call-old"}),
            (
                58,
                12823,
                "tool_start",
                {
                    "response_id": "response-call-old",
                    "tool_call_id": "fixture-old-call",
                    "name": "file_tool",
                    "arguments": {"operation": "read", "path": "fixture.txt"},
                },
            ),
            (
                58,
                12824,
                "tool_end",
                {
                    "response_id": "response-call-old",
                    "tool_call_id": "fixture-old-call",
                    "name": "file_tool",
                    "result": "fixture result",
                },
            ),
            (58, 12826, "agent_loop_start", {"turn": 61, "response_id": "response-unfinished-old"}),
            (60, 18472, "turn_end", {"message_id": 695, "turn": 67, "response_id": "response-call-visible"}),
            (
                60,
                18473,
                "tool_start",
                {
                    "response_id": "response-call-visible",
                    "tool_call_id": "fixture-visible-call",
                    "name": "file_tool",
                    "arguments": {"operation": "read", "path": "fixture.txt"},
                },
            ),
            (
                60,
                18474,
                "tool_end",
                {
                    "response_id": "response-call-visible",
                    "tool_call_id": "fixture-visible-call",
                    "name": "file_tool",
                    "result": "fixture result",
                },
            ),
            (60, 18476, "agent_loop_start", {"turn": 68, "response_id": "response-unfinished-visible"}),
            (
                60,
                18477,
                "reasoning",
                {"turn": 68, "content": "取消前旧思考", "response_id": "response-unfinished-visible"},
            ),
            (63, 1515, "turn_end", {"message_id": 718, "response_id": "fixture-current-response"}),
        ]
        if event_snapshots is None:
            event_snapshots = [] if minimal else default_event_snapshots

        db.add_all([*messages, *works])
        db.add_all(
            SessionReplyStreamEvent(
                work_id=work_id,
                sequence_no=event_sequence_no,
                event={
                    "session_id": session_id,
                    "work_id": work_id,
                    "event_sequence_no": event_sequence_no,
                    "type": event_type,
                    **payload,
                },
            )
            for work_id, event_sequence_no, event_type, payload in event_snapshots
        )
        await db.commit()

    monkeypatch.setattr(database_provider, "AsyncSessionLocal", session_factory)
    return session_id


async def _session_events(
    result_manager,
    *,
    uid,
    session_id,
    history_message_id,
    interactive_only=True,
):
    return [
        event
        async for event in result_manager.wait_for_session_stream(
            uid=uid,
            session_id=session_id,
            history_message_id=history_message_id,
            after_work_sequence_no=0,
            interactive_only=interactive_only,
        )
    ]


async def _persisted_history(session_factory, session_id):
    async with session_factory() as db:
        return await message_crud.get_history_paged(db, uid="user-1", session_id=session_id)


def _message_snapshot(messages):
    return [(message.id, message.role, message.type, message.content) for message in messages]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interactive_only", "expected_cancellations"),
    [
        pytest.param(False, [], id="normal-resume"),
        pytest.param(True, [58, 60], id="interactive-resume"),
    ],
)
async def test_cancelled_work_after_history_cursor_does_not_replay_old_tail(
    interactive_only,
    expected_cancellations,
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-latest",
    )
    result_manager = SessionReplyResult()
    old_increment_types = {"tool_start", "tool_end", "agent_loop_start", "reasoning", "content"}
    history_before = await _persisted_history(concurrent_session_factory, session_id)
    persisted_by_id = {message.id: message for message in history_before}
    assert persisted_by_id[554].type == MessageType.TOOL_CALL
    assert json.loads(persisted_by_id[554].content)["tool_calls"][0]["id"] == "fixture-old-call"
    assert persisted_by_id[555].type == MessageType.TOOL_RESULT
    assert json.loads(persisted_by_id[555].content)["content"] == "fixture result"

    replay_results = [
        await _session_events(
            result_manager,
            uid="user-1",
            session_id=session_id,
            history_message_id=718,
            interactive_only=interactive_only,
        )
        for _ in range(2)
    ]

    for events in replay_results:
        assert not any(event.get("type") in old_increment_types for event in events)
        assert [event.get("work_id") for event in events if event.get("type") == "cancelled"] == expected_cancellations
        assert all(event.get("type") == "cancelled" for event in events)
    history_after = await _persisted_history(concurrent_session_factory, session_id)
    assert _message_snapshot(history_after) == _message_snapshot(history_before)


@pytest.mark.asyncio
async def test_cancelled_tail_does_not_hide_later_successful_work(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-after-tail",
    )
    events = await _session_events(
        SessionReplyResult(),
        uid="user-1",
        session_id=session_id,
        history_message_id=695,
    )

    assert not any(event.get("work_id") == 60 and event.get("type") in {"tool_start", "tool_end", "agent_loop_start", "reasoning", "content"} for event in events)
    assert not any(event.get("type") == "turn_end" and event.get("message_id") == 695 for event in events)
    assert any(event.get("type") == "cancelled" and event.get("work_id") == 60 for event in events)
    success_end = next(index for index, event in enumerate(events) if event.get("type") == "turn_end" and event.get("work_id") == 63 and event.get("message_id") == 718)
    done = next(index for index, event in enumerate(events) if event.get("type") == "done")
    assert success_end < done


@pytest.mark.asyncio
async def test_resume_keeps_unseen_saved_turns_and_terminal(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-saved-turns",
    )
    history_before = await _persisted_history(concurrent_session_factory, session_id)
    events = await _session_events(
        SessionReplyResult(),
        uid="user-1",
        session_id=session_id,
        history_message_id=554,
    )
    work_events = [event for event in events if event.get("work_id") == 60]

    assert [event.get("type") for event in work_events] == [
        "turn_end",
        "tool_start",
        "tool_end",
        "agent_loop_start",
        "reasoning",
        "cancelled",
    ]
    assert work_events[0].get("message_id") == 695
    history_after = await _persisted_history(concurrent_session_factory, session_id)
    assert _message_snapshot(history_after) == _message_snapshot(history_before)


@pytest.mark.asyncio
async def test_direct_stream_resume_filters_only_unpersisted_tail(
    concurrent_session_factory,
    monkeypatch,
):
    await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-direct-stream",
    )

    result_manager = SessionReplyResult()
    complete_stream = [
        event
        async for event in result_manager.wait_for_stream(
            60,
            resume_mode=False,
            history_message_id=718,
        )
    ]
    resumed_stream = [
        event
        async for event in result_manager.wait_for_stream(
            60,
            resume_mode=True,
            history_message_id=718,
        )
    ]

    assert [event.get("type") for event in complete_stream] == [
        "turn_end",
        "tool_start",
        "tool_end",
        "agent_loop_start",
        "reasoning",
        "cancelled",
    ]
    assert [event.get("type") for event in resumed_stream] == ["cancelled"]


@pytest.mark.asyncio
async def test_cancelled_work_with_only_unpersisted_reasoning_resumes_terminal_only(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-reasoning-only",
        minimal=True,
        event_snapshots=[
            (60, 18477, "reasoning", {"turn": 68, "content": "unpersisted reasoning"}),
        ],
    )
    events = await _session_events(
        SessionReplyResult(),
        uid="user-1",
        session_id=session_id,
        history_message_id=0,
    )

    assert [(event.get("type"), event.get("work_id")) for event in events] == [("cancelled", 60)]


@pytest.mark.asyncio
async def test_cancelled_queued_work_without_stream_still_emits_terminal(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-queued",
        minimal=True,
    )
    events = await _session_events(
        SessionReplyResult(),
        uid="user-1",
        session_id=session_id,
        history_message_id=0,
    )

    assert [(event.get("type"), event.get("work_id")) for event in events] == [("cancelled", 60)]


@pytest.mark.asyncio
async def test_cancelled_resume_remains_isolated_by_user(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = await _seed_resume_fixture(
        concurrent_session_factory,
        monkeypatch,
        session_id="cancelled-resume-user-isolation",
    )

    events = await _session_events(
        SessionReplyResult(),
        uid="another-user",
        session_id=session_id,
        history_message_id=0,
    )

    assert events == []


@pytest.mark.asyncio
async def test_reopened_work_preserves_stream_event_rounds(
    concurrent_session_factory,
    monkeypatch,
):
    session_id = "reopened-stream-rounds"
    work_id = 801
    input_message_id = 901
    first_result_message_id = 902
    second_result_message_id = 903
    first_sequence = 4

    monkeypatch.setattr(database_provider, "AsyncSessionLocal", concurrent_session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", concurrent_session_factory)

    now = int(time.time())
    async with concurrent_session_factory() as db:
        db.add_all(
            [
                ChatSession(session_id=session_id, uid="user-1", profile_id=1, source="http"),
                Message(id=input_message_id, uid="user-1", session_id=session_id, profile_id=1, role=MessageRole.USER, type=MessageType.TEXT, content="original input must remain unchanged", is_processed=True),
                SessionReplySequence(session_id=session_id, next_sequence_no=first_sequence + 1),
                SessionReplyWorkItem(
                    id=work_id,
                    uid="user-1",
                    session_id=session_id,
                    profile_id=1,
                    sequence_no=first_sequence,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id=str(input_message_id),
                    dedupe_key=f"{session_id}:{input_message_id}",
                    status=SessionReplyWorkStatus.RUNNING,
                    input_message_ids=[input_message_id],
                    execution_state={
                        "message_source": "http",
                        "request_ids": ["round-request"],
                        "stream_requested": True,
                    },
                    locked_by="round-one-worker",
                    lock_until=now + 300,
                    attempt_count=1,
                    max_attempts=3,
                    available_at=now,
                ),
            ]
        )
        await db.commit()

    rounds = [
        {
            "worker_id": "round-one-worker",
            "response_id": "response-round-one",
            "result_message_id": first_result_message_id,
            "tool_call_id": "round-one-tool",
            "tool_name": "read",
            "turn": 1,
            "content": "round one persisted result",
        },
        {
            "worker_id": "round-two-worker",
            "response_id": "response-round-two",
            "result_message_id": second_result_message_id,
            "tool_call_id": "round-two-tool",
            "tool_name": "write",
            "turn": 2,
            "content": "round two persisted result",
        },
    ]

    async def run_round(work, round_data):
        async with concurrent_session_factory() as db:
            latest_sequence = await session_reply_stream_event_crud.get_latest_sequence(db, work_id=work_id)
        stream_state = executor_interactive_module._InteractiveWorkStreamEventState(
            work=work,
            worker_id=round_data["worker_id"],
            next_sequence=latest_sequence + 1,
            dequeued_request_ids=set(),
            turn_end_content_by_response_id={},
            tool_names_by_response_id={},
        )
        events = (
            {"type": "agent_loop_start", "turn": round_data["turn"], "response_id": round_data["response_id"]},
            {"type": "tool_start", "response_id": round_data["response_id"], "tool_call_id": round_data["tool_call_id"], "name": round_data["tool_name"]},
            {"type": "content", "response_id": round_data["response_id"], "content": round_data["content"]},
        )
        for event in events:
            persisted = await executor_interactive_module._persist_interactive_work_stream_event(stream_state, event)
            assert persisted["work_sequence_no"] == work.sequence_no

        response = {"content": round_data["content"], "response_id": round_data["response_id"], "message_id": round_data["result_message_id"], "history": [], "files": []}
        async with concurrent_session_factory() as db:
            db.add(Message(id=round_data["result_message_id"], uid="user-1", session_id=session_id, profile_id=1, role=MessageRole.ASSISTANT, type=MessageType.TEXT, content=round_data["content"], is_processed=True))
            await db.flush()
            current = await session_reply_work_item_crud.get(db, work_id)
            assert current is not None
            state = {**(current.execution_state or {}), "response": response}
            assert await session_reply_work_item_crud.update_claimed(db, work_id=work_id, worker_id=round_data["worker_id"], values={"result_message_id": round_data["result_message_id"], "execution_state": state}, commit=False)
            assert await session_reply_work_item_crud.mark_terminal(db, work_id=work_id, worker_id=round_data["worker_id"], status=SessionReplyWorkStatus.SUCCEEDED, result_message_id=round_data["result_message_id"], event_sent=True)
        return response, len(events)

    result_manager = SessionReplyResult()
    async with concurrent_session_factory() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
    assert work is not None
    outputs = []
    after_event_sequence = 0
    for index, round_data in enumerate(rounds):
        if index:
            async with concurrent_session_factory() as db:
                completed = await session_reply_work_item_crud.get(db, work_id)
                assert completed is not None and completed.status == SessionReplyWorkStatus.SUCCEEDED
                assert await terminal_watch_reply_crud.reopen_completed(db, completed)
                await db.commit()
            async with concurrent_session_factory() as db:
                work = await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id=round_data["worker_id"],
                    lease_seconds=300,
                )
            assert work is not None and work.id == work_id
            assert work.sequence_no > outputs[-1]["work_sequence_no"]

        response, event_count = await run_round(work, round_data)
        direct_stream = [
            event
            async for event in result_manager.wait_for_stream(
                work_id,
                after_sequence_no=after_event_sequence,
                resume_mode=False,
                history_message_id=0,
            )
        ]
        session_stream = [
            event
            async for event in result_manager.wait_for_session_stream(
                uid="user-1",
                session_id=session_id,
                history_message_id=0,
                after_work_sequence_no=outputs[0]["work_sequence_no"] if outputs else 0,
                interactive_only=True,
            )
        ]
        outputs.append(
            {
                "work_sequence_no": work.sequence_no,
                "event_count": event_count,
                "response": response,
                "direct": direct_stream,
                "session": session_stream,
            }
        )
        after_event_sequence = max(event["event_sequence_no"] for event in direct_stream if event.get("type") != "done")

    first, second = outputs
    first_direct_done = next(event for event in first["direct"] if event.get("type") == "done")
    second_direct_done = next(event for event in second["direct"] if event.get("type") == "done")
    first_session_done = next(event for event in first["session"] if event.get("type") == "done")
    resumed_session_done = next(event for event in second["session"] if event.get("type") == "done")
    for output in outputs:
        stream_events = [event for event in output["direct"] if event.get("type") != "done"]
        assert len(stream_events) == output["event_count"]
        assert all(event["work_sequence_no"] == output["work_sequence_no"] for event in stream_events)

    assert first_direct_done["response"] == {**first["response"], "work_id": work_id, "work_sequence_no": first["work_sequence_no"]}
    assert second_direct_done["response"] == {**second["response"], "work_id": work_id, "work_sequence_no": second["work_sequence_no"]}
    assert first_direct_done["work_sequence_no"] == first["work_sequence_no"]
    assert second_direct_done["work_sequence_no"] == second["work_sequence_no"]
    assert first_session_done["response"] == first_direct_done["response"]
    assert resumed_session_done["response"] == second_direct_done["response"]
    assert first_direct_done["response_id"] != second_direct_done["response_id"]
    assert first_direct_done["message_id"] != second_direct_done["message_id"]

    resumed_events = [event for event in second["session"] if event.get("type") != "done"]
    event_total = sum(output["event_count"] for output in outputs)
    assert [event["event_sequence_no"] for event in resumed_events] == list(range(1, event_total + 1))
    assert [event["work_sequence_no"] for event in resumed_events] == ([first["work_sequence_no"]] * first["event_count"] + [second["work_sequence_no"]] * second["event_count"])

    async with concurrent_session_factory() as db:
        status = await result_manager.get_work_status(db, work_id, uid="user-1")
        persisted_events = await session_reply_stream_event_crud.list_after_sequence(db, work_id=work_id, after_sequence_no=0)
        persisted_messages = {message_id: (await db.get(Message, message_id)).content for message_id in (input_message_id, first_result_message_id, second_result_message_id)}
    result = await result_manager.wait_for_result(work_id)
    assert status["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert status["work_sequence_no"] == second["work_sequence_no"]
    assert status["result_message_id"] == second_result_message_id
    assert status["response"] == second_direct_done["response"]
    assert result == second_direct_done["response"]
    assert [item.sequence_no for item in persisted_events] == list(range(1, event_total + 1))
    assert [item.event["work_sequence_no"] for item in persisted_events] == ([first["work_sequence_no"]] * first["event_count"] + [second["work_sequence_no"]] * second["event_count"])
    assert persisted_messages == {
        input_message_id: "original input must remain unchanged",
        first_result_message_id: "round one persisted result",
        second_result_message_id: "round two persisted result",
    }
