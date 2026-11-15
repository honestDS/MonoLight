import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, SUBAGENT_BOARD_READY_EVENT_KIND
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_delivery import build_subagent_notification_key, deliver_child_work_results
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.subagent import SubagentToolCall


async def _notification_snapshot(db, session_id: str) -> tuple[tuple[tuple[int | None, str | None, str | None], ...], tuple[tuple[int | None, str, dict], ...]]:
    messages = (await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
    events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all()
    return (
        tuple((message.id, message.dedupe_key, message.content) for message in messages),
        tuple((event.id, event.dedupe_key, deepcopy(event.event)) for event in events),
    )


@pytest.mark.asyncio
async def test_subagent_notification_identity_isolated_from_archived_keys(image_chat_runtime) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        profile_id = runtime.profile.id
        async with runtime.factory() as db:
            profile = await db.get(Profile, profile_id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_call = InternalToolCall(
            id="notification-identity-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "create",
                "name": "通知身份隔离",
                "message": "完成一个独立任务，并报告阶段进度和最终结果。",
            },
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "notification-identity-parent-request",
                "message": "创建子代理并等待它的阶段进度和最终结果。",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]
        parent_waiting = await runtime.run_next(independent_only=False)
        assert parent_waiting is not None
        assert parent_waiting.id == parent_work_id
        assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        archive_session_id = "notification-identity-archive-session"
        history_marker = "ARCHIVED_SUBAGENT_NOTIFICATION_MUST_NOT_LEAK"
        async with runtime.factory() as db:
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
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert call.id is not None and call.subagent_id == child.subagent_id and call.tool_call_id
            db.add(ChatSession(session_id=archive_session_id, uid=runtime.uid, profile_id=profile_id, source="http"))
            await db.flush()
            archive_work = SimpleNamespace(
                uid=runtime.uid,
                session_id=archive_session_id,
                profile_id=profile_id,
                dedupe_key="old-parent-identity",
                created_at=child.created_at - timedelta(days=3),
            )
            archive_board_payload = {
                "type": "subagent_board_update",
                "source": "subagent",
                "subagent_id": child.subagent_id,
                "work_id": child.id,
                "post_id": 1,
                "stage": f"{history_marker}-stage",
            }
            archive_result_payload = {
                "type": "external_work_resume",
                "source": "subagent",
                "instruction": EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT,
                "tool_call": {"id": call.tool_call_id, "name": AGENT_WORK_TOOL_NAME, "arguments": deepcopy(call.arguments)},
                "results": [{"subagent_id": call.subagent_id, "work_id": child.id, "status": "succeeded", "content": f"{history_marker}-result"}],
            }
            archive_message_specs = [
                ("board", archive_board_payload, f"{child.subagent_id}:1", f"subagent-board:{parent_work_id}:1"),
                ("result", archive_result_payload, f"{call.subagent_id}:{call.tool_call_id}", f"subagent-result:{call.id}"),
            ]
            archive_message_keys: set[str] = set()
            archive_messages = []
            for kind, payload, source, legacy_key in archive_message_specs:
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                new_key = build_subagent_notification_key(archive_work, f"subagent-{kind}", source)
                archive_message_keys.update((legacy_key, new_key))
                for dedupe_key in (legacy_key, new_key):
                    archive_messages.append(
                        Message(
                            session_id=archive_session_id,
                            uid=runtime.uid,
                            profile_id=profile_id,
                            role=MessageRole.USER,
                            type=MessageType.SUBAGENT_RESULT,
                            content=content,
                            is_processed=True,
                            dedupe_key=dedupe_key,
                        )
                    )

            archive_event = {"type": "work_ready", "session_id": archive_session_id, "work_id": parent_work_id}
            archive_event_specs = [
                ("board-ready", f"{child.subagent_id}:1", f"subagent-board-ready:{parent_work_id}:1"),
                ("work-ready", child.id, f"subagent-work-ready:{parent_work_id}:{child.id}"),
            ]
            archive_event_keys: set[str] = set()
            archive_events = []
            for kind, source, legacy_key in archive_event_specs:
                new_key = build_subagent_notification_key(archive_work, f"subagent-{kind}", source)
                archive_event_keys.update((legacy_key, new_key))
                for dedupe_key in (legacy_key, new_key):
                    archive_events.append(
                        SessionEvent(
                            dedupe_key=dedupe_key,
                            uid=runtime.uid,
                            session_id=archive_session_id,
                            event=archive_event,
                        )
                    )
            current_history_work = SimpleNamespace(
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=profile_id,
                dedupe_key=parent.dedupe_key,
                created_at=parent.created_at - timedelta(days=3),
            )
            current_history_event_payload = {
                "type": "work_ready",
                "session_id": runtime.session_id,
                "work_id": parent_work_id,
                "history_marker": history_marker,
            }
            current_history_event_key = build_subagent_notification_key(current_history_work, SUBAGENT_BOARD_READY_EVENT_KIND, f"{child.subagent_id}:1")
            current_event_key = build_subagent_notification_key(parent, SUBAGENT_BOARD_READY_EVENT_KIND, f"{child.subagent_id}:1")
            current_history_event = SessionEvent(
                dedupe_key=current_history_event_key,
                uid=runtime.uid,
                session_id=runtime.session_id,
                event=current_history_event_payload,
            )
            db.add_all([*archive_messages, *archive_events, current_history_event])
            await db.commit()

        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="notification-identity-board-post-call",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={"operation": "board_post", "stage": "核实", "content": "fresh child progress"},
                        )
                    ],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="fresh child result"),
            ]
        )
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None and child_done.id == child.id and child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            current_messages, current_events = await _notification_snapshot(db, runtime.session_id)
            archive_messages, archive_events = await _notification_snapshot(db, archive_session_id)
            assert len(current_messages) == 2
            assert len(archive_messages) == 4 and {key for _, key, _ in archive_messages} == archive_message_keys
            assert all(history_marker in (content or "") for _, _, content in archive_messages)
            assert len(archive_events) == 4 and {key for _, key, _ in archive_events} == archive_event_keys
            assert all(event == {"type": "work_ready", "session_id": archive_session_id, "work_id": parent_work_id} for _, _, event in archive_events)

            current_payloads = [json.loads(content or "{}") for _, _, content in current_messages]
            payload_by_type = {payload["type"]: payload for payload in current_payloads}
            assert payload_by_type["subagent_board_update"] == {
                "type": "subagent_board_update",
                "source": "subagent",
                "subagent_id": child.subagent_id,
                "work_id": child.id,
                "post_id": 1,
                "stage": "核实",
            }
            assert payload_by_type["external_work_resume"]["results"] == [{"subagent_id": child.subagent_id, "work_id": child.id, "status": "succeeded", "content": "fresh child result"}]
            current_keys = {key for _, key, _ in current_messages}
            assert len(current_keys) == 2 and all(isinstance(key, str) and len(key) == 64 for key in current_keys)
            assert current_keys.isdisjoint({key for _, key, _ in archive_messages})
            current_ready_events = [item for item in current_events if item[2].get("type") == "work_ready"]
            assert len(current_ready_events) == 2
            history_ready_events = [item for item in current_ready_events if item[1] == current_history_event_key]
            assert len(history_ready_events) == 1 and history_ready_events[0][2] == current_history_event_payload
            new_ready_events = [item for item in current_ready_events if item[1] != current_history_event_key]
            assert len(new_ready_events) == 1
            assert new_ready_events[0][2] == {"type": "work_ready", "session_id": runtime.session_id, "work_id": parent_work_id}
            assert new_ready_events[0][1] == current_event_key
            assert len(new_ready_events[0][1]) == 64 and new_ready_events[0][1] not in {key for _, key, _ in archive_events}

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            pending_ids = checkpoint.get("pending_resume_message_ids")
            current_message_ids = {message_id for message_id, _, _ in current_messages}
            archive_message_ids = {message_id for message_id, _, _ in archive_messages}
            assert isinstance(pending_ids, list) and set(pending_ids) == current_message_ids and not set(pending_ids) & archive_message_ids
            pending_payloads = [json.loads(content or "{}") for message_id, _, content in current_messages if message_id in pending_ids]
            assert {payload["type"] for payload in pending_payloads} == {"subagent_board_update", "external_work_resume"}
            assert any(payload.get("stage") == "核实" for payload in pending_payloads)
            assert any(payload.get("results", [{}])[0].get("content") == "fresh child result" for payload in pending_payloads)
            before = (current_messages, current_events, archive_messages, archive_events)

        async with runtime.factory() as db:
            assert await deliver_child_work_results(db, child_work_id=child.id) == 0
        async with runtime.factory() as db:
            after = await _notification_snapshot(db, runtime.session_id) + await _notification_snapshot(db, archive_session_id)
        assert after == before

        provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="fresh parent result"))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None and parent_done.id == parent_work_id and parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        resume_payload_dump = json.dumps(runtime.provider_payloads[provider_start:], ensure_ascii=False)
        assert "fresh child result" in resume_payload_dump
        assert history_marker not in resume_payload_dump
