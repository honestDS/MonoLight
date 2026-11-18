import json
from copy import deepcopy
from datetime import datetime

import pytest

from app.models.subagent import SubagentToolCall
from tests.integration.subagents import test_subagent_board_api as board_api

db_session = board_api.db_session
api_context = board_api.api_context

_add_user = board_api._add_user
_add_profile = board_api._add_profile
_add_session = board_api._add_session
_snapshot = board_api._snapshot
_add_agent_round = board_api._add_agent_round
_add_parent_work = board_api._add_parent_work
_ok = board_api._ok

SessionReplyWorkStatus = board_api.SessionReplyWorkStatus


@pytest.mark.asyncio
async def test_board_detail_selects_round_hides_terminal_posts_and_projects_result(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    old_result = _snapshot(
        2,
        status="succeeded",
        result={"content": "old result", "files": None, "skip_reply": False},
    )
    agent, _old_round, old_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="history-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=old_result,
        posts=[("plan", "old visible"), ("succeeded", "old terminal")],
    )
    latest_result = _snapshot(
        3,
        status="succeeded",
        result={
            "content": "latest result",
            "files": [{"name": "private.txt", "path": "/internal/private.txt"}],
            "skip_reply": False,
        },
    )
    await _add_agent_round(
        db,
        work_id=3,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="history-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        subagent=agent,
        round_result=latest_result,
        posts=[
            ("planning", "latest visible"),
            ("succeeded", "latest terminal"),
            ("progress", json.dumps({"full_result_operation": "query_result"})),
        ],
    )
    await db.commit()

    latest = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert latest["work_id"] == 3
    assert latest["status"] == "succeeded"
    assert latest["completed"] is True
    assert [round_data["work_id"] for round_data in latest["rounds"]] == [3, 2]
    assert [(post["stage"], post["content"]) for post in latest["posts"]] == [("planning", "latest visible")]
    assert latest["result_text"] == "latest result"
    assert latest["reason"] is None
    assert latest["ended_without_reply"] is False
    assert "files" not in latest
    assert "/internal/private.txt" not in json.dumps(latest)

    historical = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session", "work_id": old_work.id},
        )
    )
    assert historical["work_id"] == old_work.id
    assert historical["result_text"] == "old result"
    assert [(post["stage"], post["content"]) for post in historical["posts"]] == [("plan", "old visible")]


@pytest.mark.asyncio
async def test_board_detail_paginates_rounds_and_keeps_work_selection(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="round-pagination-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=_snapshot(
            2,
            status="succeeded",
            result={"content": "round-2", "files": None, "skip_reply": False},
        ),
    )
    for work_id in range(3, 25):
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name="round-pagination-agent",
            status=SessionReplyWorkStatus.SUCCEEDED,
            subagent=agent,
            round_result=_snapshot(
                work_id,
                status="succeeded",
                result={"content": f"round-{work_id}", "files": None, "skip_reply": False},
            ),
        )
    await db.commit()

    first = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    first_round_ids = [round_data["work_id"] for round_data in first["rounds"]]
    assert len(first_round_ids) == 20
    assert first_round_ids == sorted(first_round_ids, reverse=True)
    assert first_round_ids == list(range(24, 4, -1))
    assert first["next_before_round_id"] == first_round_ids[-1]

    second = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={
                "session_id": "board-api-session",
                "before_round_id": first["next_before_round_id"],
            },
        )
    )
    assert set(second) == {"rounds", "next_before_round_id"}
    second_round_ids = [round_data["work_id"] for round_data in second["rounds"]]
    assert second_round_ids == [4, 3, 2]
    assert second["next_before_round_id"] is None
    assert set(first_round_ids).isdisjoint(second_round_ids)
    assert first_round_ids + second_round_ids == list(range(24, 1, -1))

    historical = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session", "work_id": second_round_ids[-1]},
        )
    )
    assert historical["work_id"] == 2
    assert historical["result_text"] == "round-2"


@pytest.mark.asyncio
async def test_board_detail_returns_latest_fifty_posts_then_earlier_page_without_duplicates(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    long_content = "多字节正文" * 300
    agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="pagination-agent",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
        posts=[
            ("progress", "post-0"),
            ("progress", "post-1"),
            ("succeeded", "hidden-boundary"),
            *[("progress", f"post-{index}") for index in range(2, 51)],
            ("progress", long_content),
        ],
    )
    await db.commit()

    first = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert len(first["posts"]) == 50
    assert [post["content"] for post in first["posts"]] == [f"post-{index}" for index in range(2, 51)] + [long_content[:1024]]
    assert all(post["truncated"] is False for post in first["posts"][:-1])
    assert len(first["posts"][-1]["content"]) == 1024
    assert first["posts"][-1]["truncated"] is True
    assert first["next_before_post_id"] == first["posts"][0]["id"]

    second = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={
                "session_id": "board-api-session",
                "before_post_id": first["next_before_post_id"],
            },
        )
    )
    assert second["next_before_post_id"] is None
    assert [post["content"] for post in second["posts"]] == ["post-0", "post-1"]
    assert all(post["truncated"] is False for post in second["posts"])
    assert all(post["stage"] == "progress" for post in first["posts"] + second["posts"])
    assert {post["id"] for post in first["posts"]}.isdisjoint({post["id"] for post in second["posts"]})
    assert {post["content"] for post in first["posts"] + second["posts"]} == {f"post-{index}" for index in range(51)} | {long_content[:1024]}


@pytest.mark.asyncio
async def test_board_detail_projects_success_failure_and_silent_results_without_files(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    long_result = "多字节结果" * 2000
    success = _snapshot(
        2,
        status="succeeded",
        result={
            "content": long_result,
            "files": [{"path": "C:\\server\\secret.bin"}],
            "skip_reply": False,
        },
    )
    failure = _snapshot(3, status="failed", error="real failure reason")
    silent = _snapshot(
        4,
        status="succeeded",
        result={"content": None, "files": [{"path": "/srv/internal/hidden.bin"}], "skip_reply": True},
    )
    agents = {}
    for work_id, name, snapshot in ((2, "success-agent", success), (3, "failure-agent", failure), (4, "silent-agent", silent)):
        agent, _round_row, _work = await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=name,
            status=SessionReplyWorkStatus.SUCCEEDED if snapshot["status"] == "succeeded" else SessionReplyWorkStatus.FAILED,
            round_result=snapshot,
        )
        agents[name] = agent

    no_file_silent = _snapshot(
        5,
        status="succeeded",
        result={"content": None, "files": None, "skip_reply": True},
    )
    no_file_silent_agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=5,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="silent-no-files-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=no_file_silent,
        posts=[("planning", "visible before silent"), ("ended_without_reply", "")],
    )
    agents["silent-no-files-agent"] = no_file_silent_agent
    await db.commit()

    list_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "board-api-session"},
        )
    )
    no_file_silent_list = next(entry for entry in list_data["recent"] if entry["name"] == "silent-no-files-agent")
    assert no_file_silent_list["stage"] is None
    assert no_file_silent_list["preview"] == ""

    success_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['success-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert success_data["result_text"] == long_result[:8192]
    assert len(success_data["result_text"]) == 8192
    assert success_data["result_text_truncated"] is True
    assert success_data["reason"] is None
    assert success_data["reason_truncated"] is False
    assert success_data["ended_without_reply"] is False

    failure_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['failure-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert failure_data["status"] == "failed"
    assert failure_data["result_text"] is None
    assert failure_data["result_text_truncated"] is False
    assert failure_data["reason"] == "real failure reason"
    assert failure_data["reason_truncated"] is False
    assert failure_data["ended_without_reply"] is False

    silent_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['silent-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert silent_data["status"] == "succeeded"
    assert silent_data["result_text"] is None
    assert silent_data["result_text_truncated"] is False
    assert silent_data["reason"] is None
    assert silent_data["reason_truncated"] is False
    assert silent_data["ended_without_reply"] is True

    no_file_silent_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['silent-no-files-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert no_file_silent_data["status"] == "succeeded"
    assert no_file_silent_data["result_text"] is None
    assert no_file_silent_data["result_text_truncated"] is False
    assert no_file_silent_data["reason"] is None
    assert no_file_silent_data["reason_truncated"] is False
    assert no_file_silent_data["ended_without_reply"] is True
    assert [(post["stage"], post["content"]) for post in no_file_silent_data["posts"]] == [("planning", "visible before silent")]

    serialized = json.dumps({"success": success_data, "silent": silent_data})
    assert "files" not in serialized
    assert "secret.bin" not in serialized
    assert "hidden.bin" not in serialized


@pytest.mark.asyncio
async def test_board_reads_are_repeatable_and_do_not_mark_tool_delivery_or_touch_round(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    parent = await _add_parent_work(
        db,
        work_id=1,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
    )
    agent, round_row, child_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="read-only-agent",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
        posts=[("plan", "read-only preview")],
    )
    call = SubagentToolCall(
        subagent_id=agent.id,
        child_work_id=child_work.id,
        parent_work_id=parent.id,
        tool_call_id="board-api-tool-call",
        arguments={"operation": "create", "name": agent.name},
        delivered=False,
    )
    db.add(call)
    await db.commit()
    before_last_called_at = agent.last_called_at
    before_status = child_work.status
    before_execution_state = deepcopy(child_work.execution_state)
    before_round = {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": deepcopy(round_row.result),
    }

    first_list = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    second_list = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    first_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    second_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    first_delay = first_list.pop("lease_recheck_in_ms")
    second_delay = second_list.pop("lease_recheck_in_ms")
    assert 0 < second_delay <= first_delay <= 300_000
    assert first_list == second_list
    assert first_detail == second_detail

    await db.refresh(agent)
    await db.refresh(child_work)
    await db.refresh(round_row)
    await db.refresh(call)
    assert agent.last_called_at == before_last_called_at
    assert call.delivered is False
    assert child_work.status == before_status
    assert child_work.execution_state == before_execution_state
    assert {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": round_row.result,
    } == before_round


@pytest.mark.asyncio
async def test_board_reads_reflect_direct_running_to_success_and_failure_updates(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    success_agent, success_round, success_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="updated-success",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
    )
    failure_agent, failure_round, failure_work = await _add_agent_round(
        db,
        work_id=3,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="updated-failure",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
    )
    await db.commit()

    initial = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert {entry["work_id"] for entry in initial["active"]} == {2, 3}
    assert initial["recent"] == []

    success_round.result = _snapshot(
        2,
        status="succeeded",
        result={"content": "persisted success", "files": None, "skip_reply": False},
    )
    success_work.status = SessionReplyWorkStatus.SUCCEEDED
    success_work.locked_by = None
    success_work.lock_until = None
    success_work.execution_state = {
        "child_work_id": 2,
        "response": {"content": "persisted success", "files": None, "skip_reply": False},
    }
    success_work.updated_at = datetime(2026, 3, 1, 0, 0, 2)
    success_agent.active_child_work_id = None

    failure_round.result = _snapshot(3, status="failed", error="persisted failure")
    failure_work.status = SessionReplyWorkStatus.FAILED
    failure_work.locked_by = None
    failure_work.lock_until = None
    failure_work.error = "persisted failure"
    failure_work.execution_state = {"child_work_id": 3}
    failure_work.updated_at = datetime(2026, 3, 1, 0, 0, 3)
    failure_agent.active_child_work_id = None
    db.add_all([success_agent, success_round, success_work, failure_agent, failure_round, failure_work])
    await db.commit()

    updated = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert updated["active"] == []
    assert {entry["work_id"] for entry in updated["recent"]} == {2, 3}
    updated_by_work = {entry["work_id"]: entry for entry in updated["recent"]}
    assert updated_by_work[2]["status"] == "succeeded"
    assert updated_by_work[2]["completed"] is True
    assert updated_by_work[3]["status"] == "failed"
    assert updated_by_work[3]["completed"] is True

    success_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{success_agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    failure_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{failure_agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert success_detail["result_text"] == "persisted success"
    assert success_detail["reason"] is None
    assert failure_detail["result_text"] is None
    assert failure_detail["reason"] == "persisted failure"
