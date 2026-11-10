import asyncio
import json
import time
from collections.abc import AsyncGenerator
from copy import deepcopy
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_CONTEXT_INVALID,
    ERR_SUBAGENT_NOT_FOUND,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.dispatchers.context import build_dispatch_context
from app.core.i18n import t
from app.core.tools.agent_work import AgentWorkExecutor
from app.models.message import Message
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from app.models.user import User

UID = "board-owner"
SESSION_ID = "board-session"
PROFILE_ID = 1
PARENT_WORK_ID = 1
PARENT_WORKER = "board-parent-worker"
CHILD_WORKER = "board-child-worker"
BOARD_POST_FIELDS = {"id", "subagent_id", "child_work_id", "stage", "content", "created_at"}


@pytest_asyncio.fixture
async def db_session(
    database_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncSession]:
    monkeypatch.chdir(tmp_path)
    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add(User(uid=UID, username=UID))
            profile_config = ProfileConfig.model_validate({})
            profile_config.tool.enabled_tools = [AGENT_WORK_TOOL_NAME]
            db.add(
                Profile(
                    id=PROFILE_ID,
                    uid=UID,
                    name="board profile",
                    configs=profile_config.model_dump(mode="json"),
                )
            )
            db.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
            db.add(SessionReplySequence(session_id=SESSION_ID, next_sequence_no=2))
            db.add(
                SessionReplyWorkItem(
                    id=PARENT_WORK_ID,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=PROFILE_ID,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="board-user-message",
                    dedupe_key="board-parent-dedupe-key",
                    status=SessionReplyWorkStatus.RUNNING,
                    execution_state={"parent_marker": "keep"},
                    locked_by=PARENT_WORKER,
                    lock_until=int(time.time()) + 300,
                )
            )
            await db.commit()
            db.info["_board_session_factory"] = session_factory
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (PARENT_WORK_ID, PARENT_WORKER)
            yield db


def _executor(db: AsyncSession, profile: Profile, *, tool_call_id: str) -> AgentWorkExecutor:
    context = build_dispatch_context(
        mode="interactive",
        source="interactive_tool",
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        db=db,
        tool_call_id=tool_call_id,
    )
    executor = AgentWorkExecutor(project_root=".", uid=UID)
    executor.set_runtime_context(dispatch_context=context)
    return executor


async def _create_running_subagent(
    db: AsyncSession,
    profile: Profile,
    *,
    tool_call_id: str,
    name: str = "board worker",
) -> tuple[Subagent, SubagentWork, SessionReplyWorkItem, SubagentToolCall]:
    result = json.loads(
        await _executor(db, profile, tool_call_id=tool_call_id).execute(
            "create",
            name=name,
            message=f"Complete the board task for {name}.",
        )
    )
    assert result["status"] == "queued"
    assert result["operation"] == "create"
    assert result["work_id"] == result["child_work_id"]

    subagent = await db.get(Subagent, result["subagent_id"])
    child_work = await db.get(SessionReplyWorkItem, result["child_work_id"])
    round_row = await db.get(SubagentWork, result["child_work_id"])
    call = (
        await db.execute(
            select(SubagentToolCall).where(
                SubagentToolCall.subagent_id == result["subagent_id"],
                SubagentToolCall.child_work_id == result["child_work_id"],
            )
        )
    ).scalar_one()
    assert subagent is not None
    assert child_work is not None
    assert round_row is not None
    assert subagent.active_child_work_id == child_work.id
    assert round_row.current_work_id == child_work.id

    child_work.status = SessionReplyWorkStatus.RUNNING
    child_work.locked_by = CHILD_WORKER
    child_work.lock_until = int(time.time()) + 300
    db.add(child_work)
    await db.commit()
    db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work.id, CHILD_WORKER)
    return subagent, round_row, child_work, call


def _runtime_snapshot(
    parent: SessionReplyWorkItem,
    child_work: SessionReplyWorkItem,
    round_row: SubagentWork,
    call: SubagentToolCall,
) -> dict[str, object]:
    return {
        "parent": {
            "status": parent.status,
            "locked_by": parent.locked_by,
            "lock_until": parent.lock_until,
            "execution_state": deepcopy(parent.execution_state),
        },
        "child": {
            "status": child_work.status,
            "locked_by": child_work.locked_by,
            "lock_until": child_work.lock_until,
            "execution_state": deepcopy(child_work.execution_state),
        },
        "round": {
            "child_work_id": round_row.child_work_id,
            "current_work_id": round_row.current_work_id,
            "waiting_confirmation": round_row.waiting_confirmation,
            "result": deepcopy(round_row.result),
        },
        "call": {
            "child_work_id": call.child_work_id,
            "delivered": call.delivered,
            "tool_result_message_id": call.tool_result_message_id,
        },
    }


async def _message_snapshot(db: AsyncSession) -> list[tuple[object, ...]]:
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.uid == UID,
                Message.session_id == SESSION_ID,
                Message.profile_id == PROFILE_ID,
            )
            .order_by(Message.id.asc())
        )
    ).scalars()
    return [
        (
            row.id,
            row.work_id,
            str(row.role),
            str(row.type),
            str(row.content_format),
            row.content,
            row.is_processed,
        )
        for row in rows
    ]


async def _board_posts(db: AsyncSession, subagent_id: str) -> list[SubagentBoardPost]:
    return list(
        (
            await db.execute(
                select(SubagentBoardPost)
                .where(
                    SubagentBoardPost.uid == UID,
                    SubagentBoardPost.session_id == SESSION_ID,
                    SubagentBoardPost.profile_id == PROFILE_ID,
                    SubagentBoardPost.subagent_id == subagent_id,
                )
                .order_by(SubagentBoardPost.id.asc())
            )
        ).scalars()
    )


async def _execute_board_post(
    db: AsyncSession,
    profile: Profile,
    *,
    stage: str,
    content: str,
    tool_call_id: str = "board-post-call",
) -> dict:
    return json.loads(
        await _executor(db, profile, tool_call_id=tool_call_id).execute(
            "board_post",
            stage=stage,
            content=content,
        )
    )


@pytest.mark.asyncio
async def test_board_post_persists_formatted_stages_without_mutating_runtime_state(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, round_row, child_work, call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-create-call",
    )
    parent = await db_session.get(SessionReplyWorkItem, PARENT_WORK_ID)
    assert parent is not None
    before_state = _runtime_snapshot(parent, child_work, round_row, call)
    before_messages = await _message_snapshot(db_session)

    stages = [
        ("plan", "  line one\n\tline two\n\nfinal  "),
        ("verification", "second stage\n  keeps spacing"),
        ("handoff", "last stage\n\nwith a blank line"),
    ]
    returned_posts = []
    for stage, content in stages:
        result = await _execute_board_post(db_session, profile, stage=stage, content=content)
        assert result["status"] == "success"
        assert result["operation"] == "board_post"
        assert result["subagent_id"] == subagent.id
        assert result["work_id"] == child_work.id
        assert set(result["post"]) == BOARD_POST_FIELDS
        assert result["post"]["subagent_id"] == subagent.id
        assert result["post"]["child_work_id"] == child_work.id
        assert result["post"]["stage"] == stage
        assert result["post"]["content"] == content
        assert result["post"]["id"] > 0
        assert result["post"]["created_at"]
        returned_posts.append(result["post"])

    await db_session.refresh(parent)
    await db_session.refresh(child_work)
    await db_session.refresh(round_row)
    await db_session.refresh(call)
    assert _runtime_snapshot(parent, child_work, round_row, call) == before_state

    session_factory = db_session.info["_board_session_factory"]
    async with session_factory() as reader:
        persisted_posts = await _board_posts(reader, subagent.id)
        persisted_parent = await reader.get(SessionReplyWorkItem, PARENT_WORK_ID)
        persisted_child = await reader.get(SessionReplyWorkItem, child_work.id)
        persisted_round = await reader.get(SubagentWork, child_work.id)
        persisted_call = await reader.get(SubagentToolCall, call.id)
        persisted_messages = await _message_snapshot(reader)

    assert [
        post.model_dump(
            mode="json",
            include={"id", "subagent_id", "child_work_id", "stage", "content"},
        )
        for post in persisted_posts
    ] == [
        {
            "id": post["id"],
            "subagent_id": post["subagent_id"],
            "child_work_id": post["child_work_id"],
            "stage": post["stage"],
            "content": post["content"],
        }
        for post in returned_posts
    ]
    assert all(post.created_at is not None for post in persisted_posts)
    assert persisted_parent is not None
    assert persisted_child is not None
    assert persisted_round is not None
    assert persisted_call is not None
    assert _runtime_snapshot(persisted_parent, persisted_child, persisted_round, persisted_call) == before_state
    assert persisted_messages == before_messages
    assert all(content not in {message[5] for message in persisted_messages} for _, content in stages)


@pytest.mark.asyncio
async def test_board_read_returns_stable_history_empty_subagent_and_missing_error(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, round_row, child_work, call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-history-create-call",
    )
    for stage, content in (("first", "first post"), ("second", "second post")):
        result = await _execute_board_post(db_session, profile, stage=stage, content=content)
        assert result["status"] == "success"

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (PARENT_WORK_ID, PARENT_WORKER)
    empty_result = json.loads(
        await _executor(db_session, profile, tool_call_id="board-empty-create-call").execute(
            "create",
            name="empty board worker",
            message="This worker has no board posts.",
        )
    )
    assert empty_result["status"] == "queued"

    before_round = {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": deepcopy(round_row.result),
    }
    before_call = {
        "delivered": call.delivered,
        "tool_result_message_id": call.tool_result_message_id,
    }
    first_read = json.loads(
        await _executor(db_session, profile, tool_call_id="board-read-call").execute(
            "board_read",
            subagent_id=subagent.id,
        )
    )
    second_read = json.loads(
        await _executor(db_session, profile, tool_call_id="board-read-call").execute(
            "board_read",
            subagent_id=subagent.id,
        )
    )

    assert first_read == second_read
    assert first_read["status"] == "success"
    assert first_read["operation"] == "board_read"
    assert [post["stage"] for post in first_read["posts"]] == ["first", "second"]
    assert [post["content"] for post in first_read["posts"]] == ["first post", "second post"]
    assert all(set(post) == BOARD_POST_FIELDS for post in first_read["posts"])

    empty_read = json.loads(
        await _executor(db_session, profile, tool_call_id="board-empty-read-call").execute(
            "board_read",
            subagent_id=empty_result["subagent_id"],
        )
    )
    assert empty_read == {
        "status": "success",
        "operation": "board_read",
        "subagent_id": empty_result["subagent_id"],
        "posts": [],
    }

    missing_read = json.loads(
        await _executor(db_session, profile, tool_call_id="board-missing-read-call").execute(
            "board_read",
            subagent_id="f" * 32,
        )
    )
    assert missing_read["status"] == "failed"
    assert missing_read["operation"] == "board_read"
    assert missing_read["error"] == t(ERR_SUBAGENT_NOT_FOUND)

    await db_session.refresh(round_row)
    await db_session.refresh(call)
    await db_session.refresh(child_work)
    assert {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": round_row.result,
    } == before_round
    assert {
        "delivered": call.delivered,
        "tool_result_message_id": call.tool_result_message_id,
    } == before_call
    assert child_work.status == SessionReplyWorkStatus.RUNNING


@pytest.mark.asyncio
async def test_board_post_keeps_original_child_work_id_across_confirmed_continuation_and_new_round(
    db_session: AsyncSession,
) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, round_row, original_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-continuation-create-call",
    )
    initial = await _execute_board_post(db_session, profile, stage="initial", content="initial post")
    assert initial["status"] == "success"

    continuation_id = original_work.id + 1
    continuation = SessionReplyWorkItem(
        id=continuation_id,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        subagent_id=subagent.id,
        sequence_no=continuation_id,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_type=SessionReplySourceType.AUDIT_RECORD,
        source_id="1",
        dedupe_key="board-confirmed-continuation-dedupe-key",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="board-continuation-worker",
        lock_until=int(time.time()) + 300,
        execution_state={"child_work_id": original_work.id, "work_category": "subagent"},
    )
    db_session.add(continuation)
    await db_session.flush()
    round_row.current_work_id = continuation_id
    db_session.add(round_row)
    await db_session.commit()
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (continuation_id, "board-continuation-worker")

    continued = await _execute_board_post(
        db_session,
        profile,
        stage="confirmed",
        content="confirmed continuation post",
        tool_call_id="board-confirmed-post-call",
    )
    assert continued["status"] == "success"
    assert continued["work_id"] == original_work.id
    assert continued["post"]["child_work_id"] == original_work.id
    assert continuation_id != original_work.id

    continuation.status = SessionReplyWorkStatus.SUCCEEDED
    continuation.locked_by = None
    continuation.lock_until = None
    round_row.result = {"completed": True, "status": "succeeded"}
    db_session.add_all([continuation, round_row])

    next_child_id = continuation_id + 1
    next_work = SessionReplyWorkItem(
        id=next_child_id,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        subagent_id=subagent.id,
        sequence_no=next_child_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.PARENT_WORK,
        source_id=str(PARENT_WORK_ID),
        dedupe_key="board-next-round-dedupe-key",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="board-next-worker",
        lock_until=int(time.time()) + 300,
        execution_state={"child_work_id": next_child_id, "work_category": "subagent"},
    )
    db_session.add(next_work)
    await db_session.flush()
    next_round = SubagentWork(
        child_work_id=next_child_id,
        subagent_id=subagent.id,
        current_work_id=next_child_id,
    )
    db_session.add(next_round)
    subagent.active_child_work_id = next_child_id
    subagent.latest_child_work_id = next_child_id
    db_session.add(subagent)
    await db_session.commit()
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (next_child_id, "board-next-worker")

    next_result = await _execute_board_post(
        db_session,
        profile,
        stage="next-round",
        content="next round post",
        tool_call_id="board-next-post-call",
    )
    assert next_result["status"] == "success"
    assert next_result["work_id"] == next_child_id
    assert next_result["post"]["child_work_id"] == next_child_id

    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (PARENT_WORK_ID, PARENT_WORKER)
    history = json.loads(
        await _executor(db_session, profile, tool_call_id="board-cross-round-read-call").execute(
            "board_read",
            subagent_id=subagent.id,
        )
    )
    assert history["status"] == "success"
    assert [post["stage"] for post in history["posts"]] == ["initial", "confirmed", "next-round"]
    assert [post["child_work_id"] for post in history["posts"]] == [original_work.id, original_work.id, next_child_id]
    assert [post["content"] for post in history["posts"]] == [
        "initial post",
        "confirmed continuation post",
        "next round post",
    ]


@pytest.mark.asyncio
async def test_board_post_rejects_foreground_claim(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    result = await _execute_board_post(db_session, profile, stage="invalid", content="must not persist")
    assert result == {
        "status": "failed",
        "operation": "board_post",
        "error": t(ERR_SUBAGENT_CONTEXT_INVALID),
    }
    async with db_session.info["_board_session_factory"]() as reader:
        assert list((await reader.execute(select(SubagentBoardPost))).scalars()) == []


@pytest.mark.asyncio
async def test_subagent_claim_cannot_read_board(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, _round_row, _child_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-read-identity-create-call",
    )
    result = json.loads(
        await _executor(db_session, profile, tool_call_id="board-read-identity-call").execute(
            "board_read",
            subagent_id=subagent.id,
        )
    )
    assert result == {
        "status": "failed",
        "operation": "board_read",
        "error": t(ERR_SUBAGENT_CONTEXT_INVALID),
    }


@pytest.mark.parametrize("claim_case", ["missing", "not-a-tuple", "bad-work-id", "blank-worker"])
@pytest.mark.asyncio
async def test_board_post_rejects_missing_or_malformed_claim_without_writing(
    db_session: AsyncSession,
    claim_case: str,
) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, _round_row, child_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id=f"board-claim-{claim_case}-create-call",
    )
    subagent_id = subagent.id
    if claim_case == "missing":
        db_session.info.pop(SESSION_REPLY_WORK_CLAIM_INFO_KEY, None)
    elif claim_case == "not-a-tuple":
        db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = [child_work.id, CHILD_WORKER]
    elif claim_case == "bad-work-id":
        db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (str(child_work.id), CHILD_WORKER)
    else:
        db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work.id, " ")

    result = await _execute_board_post(db_session, profile, stage="invalid", content="must not persist")
    assert result == {
        "status": "failed",
        "operation": "board_post",
        "error": t(ERR_SUBAGENT_CONTEXT_INVALID),
    }
    async with db_session.info["_board_session_factory"]() as reader:
        assert await _board_posts(reader, subagent_id) == []


@pytest.mark.parametrize(
    "invalid_state",
    ["child_work_mismatch", "current_work_mismatch", "result", "waiting_confirmation", "active_child_mismatch"],
)
@pytest.mark.asyncio
async def test_board_post_rejects_invalid_round_identity_without_writing(
    db_session: AsyncSession,
    invalid_state: str,
) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, round_row, child_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id=f"board-round-{invalid_state}-create-call",
    )
    subagent_id = subagent.id
    if invalid_state == "child_work_mismatch":
        child_state = deepcopy(child_work.execution_state)
        child_state["child_work_id"] = child_work.id + 100
        child_work.execution_state = child_state
        db_session.add(child_work)
    elif invalid_state == "current_work_mismatch":
        round_row.current_work_id = child_work.id + 100
        db_session.add(round_row)
    elif invalid_state == "result":
        round_row.result = {"completed": True}
        db_session.add(round_row)
    elif invalid_state == "waiting_confirmation":
        round_row.waiting_confirmation = True
        db_session.add(round_row)
    else:
        subagent.active_child_work_id = child_work.id + 100
        db_session.add(subagent)
    await db_session.commit()
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work.id, CHILD_WORKER)

    result = await _execute_board_post(db_session, profile, stage="invalid", content="must not persist")
    assert result == {
        "status": "failed",
        "operation": "board_post",
        "error": t(ERR_SUBAGENT_CONTEXT_INVALID),
    }
    async with db_session.info["_board_session_factory"]() as reader:
        assert await _board_posts(reader, subagent_id) == []


@pytest.mark.asyncio
async def test_board_post_preserves_cancelled_semantics_when_child_lease_is_lost(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, _round_row, child_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-lease-loss-create-call",
    )
    subagent_id = subagent.id
    child_work.locked_by = "different-worker"
    db_session.add(child_work)
    await db_session.commit()
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work.id, CHILD_WORKER)

    with pytest.raises(asyncio.CancelledError):
        await _execute_board_post(db_session, profile, stage="invalid", content="must not persist")

    async with db_session.info["_board_session_factory"]() as reader:
        assert await _board_posts(reader, subagent_id) == []


@pytest.mark.asyncio
async def test_concurrent_board_post_and_cancel_preserve_cancelled_child_work(db_session: AsyncSession) -> None:
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    subagent, _round_row, child_work, _call = await _create_running_subagent(
        db_session,
        profile,
        tool_call_id="board-concurrent-create-call",
    )
    subagent_id = subagent.id
    assert child_work.id is not None
    child_work_id = child_work.id
    await db_session.commit()

    session_factory = db_session.info["_board_session_factory"]

    async def _cancel_worker() -> dict:
        async with session_factory() as db:
            cancel_profile = await db.get(Profile, PROFILE_ID)
            assert cancel_profile is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (PARENT_WORK_ID, PARENT_WORKER)
            return json.loads(
                await _executor(db, cancel_profile, tool_call_id="board-concurrent-cancel-call").execute(
                    "cancel",
                    subagent_id=subagent_id,
                    work_id=child_work_id,
                )
            )

    async def _post_worker() -> dict[str, object]:
        async with session_factory() as db:
            post_profile = await db.get(Profile, PROFILE_ID)
            assert post_profile is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work_id, CHILD_WORKER)
            try:
                return {
                    "result": await _execute_board_post(
                        db,
                        post_profile,
                        stage="concurrent",
                        content="concurrent board post",
                        tool_call_id="board-concurrent-post-call",
                    )
                }
            except asyncio.CancelledError:
                return {"cancelled": True}

    cancel_result, post_result = await asyncio.wait_for(
        asyncio.gather(_cancel_worker(), _post_worker()),
        timeout=30,
    )

    assert cancel_result["status"] == "success"
    assert cancel_result["operation"] == "cancel"
    assert cancel_result["subagent_id"] == subagent_id
    assert cancel_result["work"]["completed"] is True
    assert cancel_result["work"]["status"] == "cancelled"

    async with session_factory() as reader:
        persisted_posts = await _board_posts(reader, subagent_id)
        persisted_child = await reader.get(SessionReplyWorkItem, child_work_id)
        persisted_round = await reader.get(SubagentWork, child_work_id)

    assert persisted_child is not None
    assert persisted_child.status == SessionReplyWorkStatus.CANCELLED
    assert persisted_round is not None
    assert persisted_round.result is not None
    assert persisted_round.result["completed"] is True
    assert persisted_round.result["status"] == "cancelled"

    if post_result.get("cancelled") is True:
        assert post_result == {"cancelled": True}
        assert persisted_posts == []
    else:
        post = post_result["result"]
        assert post["status"] == "success"
        assert post["operation"] == "board_post"
        assert post["subagent_id"] == subagent_id
        assert post["work_id"] == child_work_id
        assert post["post"]["stage"] == "concurrent"
        assert post["post"]["content"] == "concurrent board post"
        assert len(persisted_posts) == 1
        assert persisted_posts[0].content == "concurrent board post"

    async with session_factory() as stale_writer:
        stale_profile = await stale_writer.get(Profile, PROFILE_ID)
        assert stale_profile is not None
        stale_writer.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (child_work_id, CHILD_WORKER)
        with pytest.raises(asyncio.CancelledError):
            await _execute_board_post(
                stale_writer,
                stale_profile,
                stage="after-cancel",
                content="must not persist",
                tool_call_id="board-after-cancel-call",
            )

    async with session_factory() as reader:
        assert len(await _board_posts(reader, subagent_id)) == len(persisted_posts)
