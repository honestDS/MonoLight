import json

import pytest

import app.providers.database as database_provider
from app.core.crud.session.message import message_crud
from app.core.session_reply_queue.manager_result import SessionReplyResult
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
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
