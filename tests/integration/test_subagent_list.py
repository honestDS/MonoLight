import json
import time
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_ARGUMENTS_INVALID,
    ERR_SUBAGENT_CONTEXT_INVALID,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.dispatchers.context import build_dispatch_context
from app.core.i18n import t
from app.core.tools.agent_work import AgentWorkExecutor
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageContentFormat, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.models.user import User


@pytest_asyncio.fixture
async def db_session(
    database_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncSession]:
    monkeypatch.chdir(tmp_path)
    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add(User(uid="list-owner", username="list-owner"))
            await db.flush()

            profile_config = ProfileConfig.model_validate({})
            profile_config.tool.enabled_tools = [AGENT_WORK_TOOL_NAME]
            db.add(
                Profile(
                    id=1,
                    uid="list-owner",
                    name="list profile",
                    configs=profile_config.model_dump(mode="json"),
                )
            )
            await db.flush()

            db.add(
                ChatSession(
                    session_id="list-session",
                    uid="list-owner",
                    profile_id=1,
                )
            )
            await db.flush()

            db.add(
                SessionReplyWorkItem(
                    id=1,
                    uid="list-owner",
                    session_id="list-session",
                    profile_id=1,
                    sequence_no=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="1",
                    dedupe_key="list-parent-dedupe-key",
                    status=SessionReplyWorkStatus.RUNNING,
                    locked_by="list-worker",
                    lock_until=int(time.time()) + 300,
                )
            )
            await db.commit()
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (1, "list-worker")
            yield db


def _executor(db: AsyncSession, profile: Profile) -> AgentWorkExecutor:
    context = build_dispatch_context(
        mode="interactive",
        source="interactive_tool",
        uid="list-owner",
        session_id="list-session",
        profile=profile,
        db=db,
        tool_call_id="list-call",
    )
    executor = AgentWorkExecutor(project_root=".", uid="list-owner")
    executor.set_runtime_context(dispatch_context=context)
    return executor


async def _add_subagent(
    db: AsyncSession,
    *,
    work_id: int,
    name: str,
    status: SessionReplyWorkStatus,
    active: bool = True,
    archived: bool = False,
    uid: str = "list-owner",
    session_id: str = "list-session",
    profile_id: int = 1,
    lock_until: int | None = None,
    waiting_confirmation: bool = False,
    execution_state: dict | None = None,
    add_tool_call: bool = False,
) -> tuple[Subagent, SubagentWork, SessionReplyWorkItem]:
    subagent_id = f"{work_id:032x}"
    created_at = datetime(2025, 1, 1) + timedelta(seconds=work_id)
    subagent = Subagent(
        id=subagent_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        name=name,
        active_child_work_id=work_id if active else None,
        latest_child_work_id=work_id,
        last_called_at=created_at,
        created_at=created_at,
    )
    db.add(subagent)
    await db.flush()

    child_state = {"child_work_id": work_id}
    if execution_state is not None:
        child_state.update(execution_state)
    if status == SessionReplyWorkStatus.RUNNING and lock_until is None:
        lock_until = int(time.time()) + 300
    work = SessionReplyWorkItem(
        id=work_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        subagent_id=subagent_id,
        sequence_no=work_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.PARENT_WORK,
        source_id="1",
        dedupe_key=f"list-child-{work_id}-dedupe-key",
        status=status,
        execution_state=child_state,
        lock_until=lock_until,
    )
    db.add(work)
    await db.flush()

    round_row = SubagentWork(
        child_work_id=work_id,
        subagent_id=subagent_id,
        current_work_id=work_id,
        waiting_confirmation=waiting_confirmation,
        result=None,
        created_at=created_at,
    )
    if archived:
        round_row.result = {
            "work_id": work_id,
            "current_work_id": work_id,
            "completed": True,
            "status": "succeeded",
            "result": {"content": None, "files": None, "skip_reply": True},
            "error": None,
        }
    db.add(round_row)
    await db.flush()

    if add_tool_call:
        db.add(
            SubagentToolCall(
                subagent_id=subagent_id,
                child_work_id=work_id,
                parent_work_id=1,
                tool_call_id=f"list-tool-call-{work_id}",
                arguments={"operation": "create", "name": name, "message": f"Work for {name}"},
            )
        )
        await db.flush()

    return subagent, round_row, work


async def _child_state_dump(db: AsyncSession) -> dict[str, list[dict]]:
    entities = (
        ("subagents", select(Subagent).order_by(Subagent.id)),
        ("rounds", select(SubagentWork).order_by(SubagentWork.child_work_id)),
        (
            "works",
            select(SessionReplyWorkItem).where(SessionReplyWorkItem.subagent_id.is_not(None)).order_by(SessionReplyWorkItem.id),
        ),
        ("tool_calls", select(SubagentToolCall).order_by(SubagentToolCall.id)),
        ("messages", select(Message).order_by(Message.id)),
    )
    snapshot = {}
    for name, statement in entities:
        rows = (await db.execute(statement.execution_options(populate_existing=True))).scalars().all()
        snapshot[name] = [row.model_dump() for row in rows]
    return snapshot


@pytest.mark.asyncio
async def test_list_projects_real_active_child_state_read_only_and_stable(db_session: AsyncSession) -> None:
    expected = [
        (8, "ready", SessionReplyWorkStatus.READY_FOR_LLM, "ready_for_llm"),
        (9, "running", SessionReplyWorkStatus.RUNNING, "running"),
        (10, "waiting", SessionReplyWorkStatus.WAITING_EXTERNAL_WORK, "waiting_external_work"),
        (11, "expired", SessionReplyWorkStatus.RUNNING, "execution_unknown"),
    ]
    for work_id, name, status, _snapshot_status in reversed(expected):
        await _add_subagent(
            db_session,
            work_id=work_id,
            name=name,
            status=status,
            lock_until=0 if work_id == 11 else None,
            add_tool_call=True,
        )

    await _add_subagent(
        db_session,
        work_id=12,
        name="idle",
        status=SessionReplyWorkStatus.SUCCEEDED,
        active=False,
        execution_state={"response": {"skip_reply": True}},
        add_tool_call=True,
    )
    await _add_subagent(
        db_session,
        work_id=13,
        name="archived",
        status=SessionReplyWorkStatus.SUCCEEDED,
        archived=True,
        add_tool_call=True,
    )
    for work_id, name, _status, _snapshot_status in expected:
        db_session.add(
            Message(
                session_id="list-session",
                uid="list-owner",
                profile_id=1,
                work_id=work_id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content_format=MessageContentFormat.TEXT,
                content=f"Instruction for {name}",
            )
        )
    await db_session.flush()

    before = await _child_state_dump(db_session)
    profile = await db_session.get(Profile, 1)
    assert profile is not None
    executor = _executor(db_session, profile)

    first = json.loads(await executor.execute("list"))
    second = json.loads(await executor.execute("list"))

    assert first["status"] == "success"
    assert first["operation"] == "list"
    assert first == second
    entries = first["subagents"]
    assert len(entries) == len(expected)
    for entry, (work_id, name, _status, snapshot_status) in zip(entries, expected, strict=True):
        assert entry["name"] == name
        assert entry["subagent_id"] == f"{work_id:032x}"
        assert entry["work"]["work_id"] == work_id
        assert entry["work"]["current_work_id"] == work_id
        assert entry["work"]["completed"] is False
        assert entry["work"]["status"] == snapshot_status

    after = await _child_state_dump(db_session)
    assert after == before
    assert len(after["tool_calls"]) == 6
    assert all(call["delivered"] is False for call in after["tool_calls"])


@pytest.mark.parametrize(
    ("status", "execution_state"),
    [
        (SessionReplyWorkStatus.SUCCEEDED, {"response": {"skip_reply": True}}),
        (SessionReplyWorkStatus.FAILED, {}),
        (SessionReplyWorkStatus.CANCELLED, {}),
    ],
)
@pytest.mark.asyncio
async def test_list_filters_terminal_child_work_without_archiving_or_clearing_pointer(
    db_session: AsyncSession,
    status: SessionReplyWorkStatus,
    execution_state: dict,
) -> None:
    subagent, round_row, work = await _add_subagent(
        db_session,
        work_id=2,
        name=f"terminal-{status.value}",
        status=status,
        execution_state=execution_state,
    )
    await db_session.flush()
    profile = await db_session.get(Profile, 1)
    assert profile is not None

    result = json.loads(await _executor(db_session, profile).execute("list"))

    assert result["status"] == "success"
    assert result["subagents"] == []
    await db_session.refresh(subagent)
    await db_session.refresh(round_row)
    await db_session.refresh(work)
    assert subagent.active_child_work_id == work.id
    assert round_row.result is None
    assert work.status == status


@pytest.mark.asyncio
async def test_list_keeps_pending_confirmation_unfinished_and_filters_rejected_record(db_session: AsyncSession) -> None:
    subagent, round_row, work = await _add_subagent(
        db_session,
        work_id=2,
        name="needs confirmation",
        status=SessionReplyWorkStatus.SUCCEEDED,
        waiting_confirmation=True,
    )
    source_message = Message(
        session_id="list-session",
        uid="list-owner",
        profile_id=1,
        work_id=work.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content_format=MessageContentFormat.TEXT,
        content="{}",
    )
    db_session.add(source_message)
    await db_session.flush()
    record = AuditRecord(
        uid="list-owner",
        operator_username="list-owner",
        session_id="list-session",
        source="interactive_tool",
        language="en",
        status=AuditRecordStatus.PENDING,
        source_work_id=work.id,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="list-confirmation-hash",
        tool_count=1,
    )
    db_session.add(record)
    await db_session.flush()
    confirmation_message = Message(
        session_id="list-session",
        uid="list-owner",
        profile_id=1,
        work_id=work.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.AUDIT_CONFIRMATION,
        content_format=MessageContentFormat.TEXT,
        content=json.dumps({"audit_record_id": record.id}),
    )
    db_session.add(confirmation_message)
    await db_session.flush()
    work.result_message_id = confirmation_message.id
    await db_session.commit()

    profile = await db_session.get(Profile, 1)
    assert profile is not None
    executor = _executor(db_session, profile)
    pending = json.loads(await executor.execute("list"))

    assert pending["status"] == "success"
    assert len(pending["subagents"]) == 1
    pending_work = pending["subagents"][0]["work"]
    assert pending_work["work_id"] == 2
    assert pending_work["current_work_id"] == work.id
    assert pending_work["status"] == "waiting_confirmation"
    assert pending_work["completed"] is False

    record.status = AuditRecordStatus.REJECTED
    await db_session.commit()
    rejected_record_state = record.model_dump(mode="json")
    confirmation_round_state = round_row.model_dump(mode="json")
    rejected = json.loads(await executor.execute("list"))

    await db_session.refresh(record)
    await db_session.refresh(round_row)
    await db_session.refresh(subagent)
    assert rejected["status"] == "success"
    assert rejected["subagents"] == []
    assert record.model_dump(mode="json") == rejected_record_state
    assert round_row.model_dump(mode="json") == confirmation_round_state
    assert round_row.waiting_confirmation is True
    assert round_row.result is None
    assert subagent.active_child_work_id == work.id


@pytest.mark.asyncio
async def test_list_projects_confirmed_continuation_work_as_current_snapshot(db_session: AsyncSession) -> None:
    subagent, round_row, old_work = await _add_subagent(
        db_session,
        work_id=2,
        name="confirmed continuation",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={"response": {"skip_reply": True}},
    )
    continued_work = SessionReplyWorkItem(
        id=3,
        uid="list-owner",
        session_id="list-session",
        profile_id=1,
        subagent_id=subagent.id,
        sequence_no=3,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_type=SessionReplySourceType.AUDIT_RECORD,
        source_id="1",
        dedupe_key="list-confirmed-continuation-dedupe-key",
        status=SessionReplyWorkStatus.RUNNING,
        lock_until=int(time.time()) + 300,
        execution_state={"child_work_id": old_work.id, "work_category": "subagent"},
    )
    db_session.add(continued_work)
    await db_session.flush()
    round_row.current_work_id = continued_work.id
    await db_session.commit()

    profile = await db_session.get(Profile, 1)
    assert profile is not None
    result = json.loads(await _executor(db_session, profile).execute("list"))

    assert result["status"] == "success"
    assert len(result["subagents"]) == 1
    entry = result["subagents"][0]
    assert entry["subagent_id"] == subagent.id
    assert entry["name"] == "confirmed continuation"
    assert entry["work"]["work_id"] == old_work.id
    assert entry["work"]["current_work_id"] == continued_work.id
    assert entry["work"]["status"] == "running"
    assert entry["work"]["completed"] is False


@pytest.mark.asyncio
async def test_list_isolated_to_user_session_and_profile_scope(db_session: AsyncSession) -> None:
    visible, _, _ = await _add_subagent(
        db_session,
        work_id=2,
        name="visible",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
    )

    other_session_name = "private-session-name"
    other_session_id = "private-session-id"
    db_session.add(
        ChatSession(
            session_id=other_session_id,
            uid="list-owner",
            profile_id=1,
            title=other_session_name,
        )
    )
    await db_session.flush()
    other_session_agent, _, _ = await _add_subagent(
        db_session,
        work_id=3,
        name="private-other-session-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        session_id=other_session_id,
    )

    db_session.add(
        Profile(
            id=2,
            uid="list-owner",
            name="private-profile-name",
            configs=ProfileConfig.model_validate({}).model_dump(mode="json"),
        )
    )
    await db_session.flush()
    other_profile_agent, _, _ = await _add_subagent(
        db_session,
        work_id=4,
        name="private-other-profile-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        profile_id=2,
    )

    db_session.add(User(uid="other-owner", username="other-owner"))
    await db_session.flush()
    db_session.add(
        Profile(
            id=3,
            uid="other-owner",
            name="private-other-user-profile",
            configs=ProfileConfig.model_validate({}).model_dump(mode="json"),
        )
    )
    await db_session.flush()
    other_user_session_name = "private-other-user-session-name"
    other_user_session_id = "private-other-user-session"
    db_session.add(
        ChatSession(
            session_id=other_user_session_id,
            uid="other-owner",
            profile_id=3,
            title=other_user_session_name,
        )
    )
    await db_session.flush()
    other_user_agent, _, _ = await _add_subagent(
        db_session,
        work_id=5,
        name="private-other-user-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        uid="other-owner",
        session_id=other_user_session_id,
        profile_id=3,
    )

    private_rows = (
        (other_session_agent, "private-instruction-other-session"),
        (other_profile_agent, "private-instruction-other-profile"),
        (other_user_agent, "private-instruction-other-user"),
    )
    for subagent, instruction in private_rows:
        db_session.add(
            Message(
                session_id=subagent.session_id,
                uid=subagent.uid,
                profile_id=subagent.profile_id,
                work_id=subagent.active_child_work_id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content_format=MessageContentFormat.TEXT,
                content=instruction,
            )
        )
    await db_session.flush()

    before = await _child_state_dump(db_session)
    profile = await db_session.get(Profile, 1)
    assert profile is not None
    result = json.loads(await _executor(db_session, profile).execute("list"))
    serialized_result = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "success"
    assert result["operation"] == "list"
    assert len(result["subagents"]) == 1
    assert result["subagents"][0]["subagent_id"] == visible.id
    assert result["subagents"][0]["name"] == "visible"
    private_values = [
        other_session_name,
        other_session_id,
        other_session_agent.id,
        other_session_agent.name,
        "private-instruction-other-session",
        other_profile_agent.id,
        other_profile_agent.name,
        "private-profile-name",
        "private-instruction-other-profile",
        other_user_session_name,
        other_user_session_id,
        other_user_agent.id,
        other_user_agent.name,
        "private-other-user-profile",
        "private-instruction-other-user",
    ]
    for private_value in private_values:
        assert private_value not in serialized_result

    after = await _child_state_dump(db_session)
    assert after == before


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.asyncio
async def test_list_during_parent_child_conversation_reports_only_unfinished_subagents(image_chat_runtime, protocol: str) -> None:
    create_calls = [
        InternalToolCall(
            id=f"subagent-list-create-{name}",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": name, "message": f"Complete independent task {name}."},
        )
        for name in ("first", "second")
    ]

    async def persisted_list_results(db: AsyncSession, *, uid: str, session_id: str, profile_id: int) -> list[tuple[InternalMessage, dict]]:
        rows = (
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.uid == uid,
                        Message.session_id == session_id,
                        Message.profile_id == profile_id,
                        Message.work_id.is_(None),
                        Message.role == MessageRole.TOOL,
                        Message.type == MessageType.TOOL_RESULT,
                    )
                    .order_by(Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
        results = []
        for row in rows:
            message = InternalMessage.model_validate_json(row.content or "{}")
            payload = json.loads(message.content or "{}")
            if payload.get("operation") == "list":
                results.append((message, payload))
        return results

    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls),
                InternalMessage(role=MessageRole.ASSISTANT, content="Both independent tasks are ready."),
            ]
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "subagent-list-lifecycle-request",
                "message": "Create two independent tasks and later list the unfinished task.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        parent_created = await runtime.run_next(independent_only=False)
        assert parent_created is not None
        assert parent_created.id == parent_work_id
        assert parent_created.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == 2

        async with runtime.factory() as db:
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 2
            child_by_id = {call.child_work_id: (call.subagent_id, call.arguments["name"]) for call in calls}
            assert len(child_by_id) == 2

        first_summary = "first-independent-task-result"
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        first_done = await runtime.run_next(independent_only=True)
        assert first_done is not None
        assert first_done.status == SessionReplyWorkStatus.SUCCEEDED
        first_child_work_id = first_done.id
        assert first_child_work_id in child_by_id
        pending_child_work_id = next(child_id for child_id in child_by_id if child_id != first_child_work_id)
        pending_subagent_id, pending_name = child_by_id[pending_child_work_id]

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM

        list_call = InternalToolCall(
            id="subagent-list-first-list-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "list"},
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[list_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="The first result is ready; the second task continues."),
            ]
        )
        list_provider_start = len(runtime.provider_payloads)
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        list_provider_payloads = runtime.provider_payloads[list_provider_start:]
        assert len(list_provider_payloads) == 2

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 2
            assert all(call.arguments["operation"] == "create" for call in calls)
            pending_call = next(call for call in calls if call.child_work_id == pending_child_work_id)
            assert pending_call.delivered is False
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert {slot.get("child_work_id") for slot in waits if isinstance(slot, dict)} == {pending_child_work_id}
            list_results = await persisted_list_results(db, uid=runtime.uid, session_id=runtime.session_id, profile_id=runtime.profile.id)
            assert len(list_results) == 1
            list_result_message, first_list_result = list_results[0]
            assert list_result_message.tool_execution_status == "succeeded"
            assert first_list_result["status"] == "success"
            assert first_list_result["operation"] == "list"
            assert len(first_list_result["subagents"]) == 1
            assert first_list_result["subagents"][0]["subagent_id"] == pending_subagent_id
            assert first_list_result["subagents"][0]["name"] == pending_name
            assert first_list_result["subagents"][0]["work"]["work_id"] == pending_child_work_id
            assert first_list_result["subagents"][0]["work"]["status"] == "ready_for_llm"

        second_summary = "second-independent-task-result"
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_summary))
        second_done = await runtime.run_next(independent_only=True)
        assert second_done is not None
        assert second_done.id == pending_child_work_id
        assert second_done.status == SessionReplyWorkStatus.SUCCEEDED

        second_list_call = InternalToolCall(
            id="subagent-list-second-list-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "list"},
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[second_list_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="Both independent tasks are complete."),
            ]
        )
        final_parent = await runtime.run_next(independent_only=False)
        assert final_parent is not None
        assert final_parent.id == parent_work_id
        assert final_parent.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            create_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            pending_call = next(call for call in create_calls if call.child_work_id == pending_child_work_id)
            assert pending_call.delivered is True
            assert pending_call.tool_result_message_id is not None
            child_result_row = await db.get(Message, pending_call.tool_result_message_id)
            assert child_result_row is not None
            child_result_message = InternalMessage.model_validate_json(child_result_row.content or "{}")
            child_result_payload = json.loads(child_result_message.content or "{}")
            assert child_result_message.tool_execution_status == "succeeded"
            assert child_result_payload["status"] == "success"
            assert child_result_payload["work"]["result"]["content"] == second_summary
            final_list_results = await persisted_list_results(db, uid=runtime.uid, session_id=runtime.session_id, profile_id=runtime.profile.id)
            assert len(final_list_results) == 2
            assert final_list_results[0][0].tool_execution_status == "succeeded"
            assert final_list_results[0][1] == first_list_result
            final_list_result_message, final_list_result = final_list_results[-1]
            assert final_list_result_message.tool_execution_status == "succeeded"
            assert final_list_result["status"] == "success"
            assert final_list_result["operation"] == "list"
            assert final_list_result["subagents"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("work_id", 2), ("profile_id", 2)],
    ids=["other-operation-field", "scope-override-field"],
)
@pytest.mark.asyncio
async def test_list_rejects_unexpected_arguments(db_session: AsyncSession, field: str, value: int) -> None:
    profile = await db_session.get(Profile, 1)
    assert profile is not None

    result = json.loads(await _executor(db_session, profile).execute("list", **{field: value}))

    assert result["status"] == "failed"
    assert result["operation"] == "list"
    assert result["error"] == t(ERR_SUBAGENT_ARGUMENTS_INVALID)
    assert result["details"] == [{"field": field, "type": "extra_forbidden"}]
    assert "subagents" not in result


@pytest.mark.asyncio
async def test_list_requires_a_valid_foreground_parent_claim(db_session: AsyncSession) -> None:
    subagent, _, _ = await _add_subagent(
        db_session,
        work_id=2,
        name="claim-required",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
    )
    subagent_id = subagent.id
    await db_session.commit()
    before = await _child_state_dump(db_session)
    profile = await db_session.get(Profile, 1)
    assert profile is not None
    del db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY]

    result = json.loads(await _executor(db_session, profile).execute("list"))
    serialized_result = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "failed"
    assert result["operation"] == "list"
    assert result["error"] == t(ERR_SUBAGENT_CONTEXT_INVALID)
    assert "subagents" not in result
    assert subagent_id not in serialized_result
    after = await _child_state_dump(db_session)
    assert after == before
