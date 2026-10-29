import json
from typing import Any

import pytest
from sqlmodel import select

import app.core.background_tasks.external_work as external_work_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.providers.database as database_provider
from app.core.constants import SHELL_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from app.providers.llm.client import LLMClient

UID = "result-delivery-user"
SESSION_ID = "result-delivery-session"
PROFILE_ID = 1
MODEL_ID = "result-delivery-model"
WORKER_ID = "result-delivery-worker"


async def _seed_suspended_source(factory, *, label: str, result_marker: str) -> dict[str, Any]:
    source_text = f"请执行命令 {label}"
    request_id = f"source-request-{label}"
    call_id = f"source-call-{label}"
    async with factory() as db:
        profile = await db.get(Profile, PROFILE_ID)
        assert profile is not None
        user_message, work = await session_reply_queue_manager.enqueue_foreground_message(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile=profile,
            message=source_text,
            attachments=None,
            source="http",
            stream_requested=False,
            context_summary_events_requested=False,
            request_id=request_id,
        )
        assert work.id is not None and user_message.id is not None

        task = BackgroundTask(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id=call_id,
            tool_name=SHELL_TOOL_NAME,
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"command": f"echo {result_marker}", "execution_mode": "non_interactive"},
            result={"content": {"stdout": result_marker, "exit_code": 0}},
            auto_reply=False,
            extra={"origin_work_id": work.id, "origin_work_identity": work.dedupe_key, "request_context": [{"id": user_message.id, "role": "user", "content": source_text}]},
        )
        db.add(task)
        await db.flush()
        assert task.id is not None

        assistant_call = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[InternalToolCall(id=call_id, name=SHELL_TOOL_NAME, arguments=dict(task.arguments))])
        saved_call = await save_message(db, SESSION_ID, UID, MessageRole.ASSISTANT, MessageType.TOOL_CALL, assistant_call, PROFILE_ID, is_processed=True, commit=False)
        queued_payload = {"status": "queued", "task_id": task.id}
        tool_result = InternalMessage(role=MessageRole.TOOL, tool_call_id=call_id, content=json.dumps(queued_payload, ensure_ascii=False))
        saved_result = await save_message(db, SESSION_ID, UID, MessageRole.TOOL, MessageType.TOOL_RESULT, tool_result, PROFILE_ID, is_processed=True, commit=False)
        assert saved_call.id is not None and saved_result.id is not None

        checkpoint_messages = [
            user_message.model_dump(mode="json", exclude_none=True),
            assistant_call.model_copy(update={"id": saved_call.id}).model_dump(mode="json", exclude_none=True),
            tool_result.model_copy(update={"id": saved_result.id}).model_dump(mode="json", exclude_none=True),
        ]
        slot = {
            "task_id": task.id,
            "tool_call_id": call_id,
            "tool_name": SHELL_TOOL_NAME,
            "tool_result_message_id": saved_result.id,
            "response_id": f"response-{label}",
        }
        work.input_message_ids = [user_message.id]
        work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        work.execution_state = {
            **(work.execution_state or {}),
            "request_ids": [request_id],
            "show_tool_calls": True,
            "expose_tool_call_content": True,
            "pending_tools": [slot],
            "external_tool_result_message_ids": [saved_result.id],
            "dispatcher_checkpoint": {
                "messages": checkpoint_messages,
                "turn_messages": checkpoint_messages,
                "reload_history": False,
                "current_turn": 1,
            },
        }
        user_row = await db.get(Message, user_message.id)
        assert user_row is not None
        user_row.is_processed = True
        await db.commit()
        return {
            "work_id": work.id,
            "user_id": user_message.id,
            "task_id": task.id,
            "call_message_id": saved_call.id,
            "result_message_id": saved_result.id,
            "call_id": call_id,
            "source_text": source_text,
            "request_id": request_id,
        }


def _resume_payloads(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for message in messages:
        if message.role != MessageRole.USER or not isinstance(message.content, str):
            continue
        try:
            materialized = json.loads(message.content)
            nested = materialized.get("user_message") if isinstance(materialized, dict) else None
            payload = json.loads(nested) if isinstance(nested, str) else None
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "external_work_resume":
            payloads.append(payload)
    return payloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved_input",
    [pytest.param(False, id="fresh-input"), pytest.param(True, id="saved-input")],
)
async def test_result_delivery_merges_two_suspended_sources_into_latest_foreground_work(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    saved_input: bool,
) -> None:
    captured_requests: list[list[InternalMessage]] = []
    sent_events: list[tuple[str, str, dict[str, Any]]] = []
    latest_text = "你应该查一下，合并告诉我这两个结果"
    final_text = "两个命令结果已合并"

    async def fake_generate(_client_cls, **kwargs: Any) -> InternalResponse:
        captured_requests.append([message.model_copy(deep=True) for message in kwargs["messages"]])
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=final_text),
            model=MODEL_ID,
            finish_reason="stop",
        )

    async def capture_send_session_event(uid: str, session_id: str, event: dict[str, Any]) -> None:
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(LLMClient, "generate", classmethod(fake_generate))

    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="result-delivery-workflow.sqlite3",
    ) as factory:
        channel = ModelChannel(
            id=1,
            name="result-delivery-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid",
            model_ids=[
                {
                    "model_id": MODEL_ID,
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 32,
                    "max_tokens": 512,
                }
            ],
        )
        profile = Profile(
            id=PROFILE_ID,
            uid=UID,
            name="result-delivery-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {"chat_channel": {"rules": [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]}},
                    "security": {"audit_threshold": 0},
                    "tool": {"enabled_tools": [SHELL_TOOL_NAME]},
                    "other": {},
                    "memory": {"enabled": False},
                }
            ).model_dump(mode="json"),
        )
        user = User(uid=UID, username="result-delivery-user")
        session = ChatSession(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            source="http",
            goal_mode=False,
            stream_llm_request=False,
            show_tool_calls=True,
        )
        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.commit()

        for module in (
            database_provider,
            dispatcher_module,
            executor_interactive_module,
            executor_lifecycle_module,
            executor_metadata_module,
            channel_cursor_module,
            external_work_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_send_session_event)

        source_a = await _seed_suspended_source(factory, label="A", result_marker="result-A")
        source_b = await _seed_suspended_source(factory, label="B", result_marker="result-B")
        frozen_message_ids: list[int] | None = None

        async with factory() as db:
            profile = await db.get(Profile, PROFILE_ID)
            assert profile is not None
            latest_user, target = await session_reply_queue_manager.enqueue_foreground_message(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile=profile,
                message=latest_text,
                attachments=None,
                source="http",
                stream_requested=False,
                context_summary_events_requested=False,
                request_id="latest-request",
            )
            assert latest_user.id is not None and target.id is not None
            target_id = target.id
            if saved_input:
                persisted_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
                frozen_history = parse_db_messages_to_internal(persisted_messages)
                frozen_message_ids = [message.id for message in frozen_history]
                assert frozen_message_ids == [message.id for message in persisted_messages]
                target.input_message_ids = [latest_user.id]
                target.attempt_count = 1
                target.execution_state = {
                    **(target.execution_state or {}),
                    "dispatcher_checkpoint": {
                        "messages": [message.model_dump(mode="json") for message in frozen_history],
                        "turn_messages": [],
                        "current_turn": 0,
                        "reload_history": False,
                    },
                }
                persisted_latest = await db.get(Message, latest_user.id)
                assert persisted_latest is not None
                persisted_latest.is_processed = True
                await db.commit()

        async with factory() as db:
            claimed = await session_reply_work_item_crud.claim_next(db, worker_id=WORKER_ID, lease_seconds=300)
            assert claimed is not None and claimed.id == target_id
        await executor_lifecycle_module.execute_session_reply_work(target_id, WORKER_ID)

        assert len(captured_requests) == 1
        request_messages = captured_requests[0]
        assert any(message.role == MessageRole.USER and latest_text in str(message.content) for message in request_messages)
        assert request_messages[-1].role != MessageRole.ASSISTANT
        resumes = _resume_payloads(request_messages)
        assert len(resumes) == 2
        task_ids = [source_a["task_id"], source_b["task_id"]]
        resume_tasks = [task for payload in resumes for task in payload["tasks"]]
        assert sorted(task["task_id"] for task in resume_tasks) == sorted(task_ids)
        assert sorted(task["task"]["stdout"] for task in resume_tasks) == ["result-A", "result-B"]
        assert all(isinstance(payload.get("reply_guidance"), str) and payload["reply_guidance"].strip() for payload in resumes)
        for source in (source_a, source_b):
            call_messages = [message for message in request_messages if message.role == MessageRole.ASSISTANT and any(tool_call.id == source["call_id"] for tool_call in message.tool_calls or [])]
            result_messages = [message for message in request_messages if message.role == MessageRole.TOOL and message.tool_call_id == source["call_id"]]
            assert len(call_messages) == 1 and len(result_messages) == 1
            assert json.loads(result_messages[0].content or "{}")["status"] == "queued"
        if saved_input:
            assert frozen_message_ids is not None
            assert [message.id for message in request_messages[: len(frozen_message_ids)]] == frozen_message_ids

        async with factory() as db:
            works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            target_work = await db.get(SessionReplyWorkItem, target_id)
            assert target_work is not None and target_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(works) == 3 and len(tasks) == 2
            sources = {source["work_id"]: await db.get(SessionReplyWorkItem, source["work_id"]) for source in (source_a, source_b)}
            assert all(source is not None and source.status == SessionReplyWorkStatus.MERGED and source.merged_into_id == target_id for source in sources.values())
            assert set(target_work.execution_state["request_ids"]) >= {source_a["request_id"], source_b["request_id"], "latest-request"}

            final_messages = [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
            assert len(final_messages) == 1 and final_messages[0].content == final_text
            assert target_work.result_message_id == final_messages[0].id
            assert latest_user.id is not None
            persisted_latest = await db.get(Message, latest_user.id)
            assert persisted_latest is not None and persisted_latest.content == latest_text
            for source in (source_a, source_b):
                persisted_user = await db.get(Message, source["user_id"])
                assert persisted_user is not None and persisted_user.content == source["source_text"]
                assert persisted_user.is_processed is True
                call_row = await db.get(Message, source["call_message_id"])
                result_row = await db.get(Message, source["result_message_id"])
                assert call_row is not None and result_row is not None
                persisted_call_payload = InternalMessage.model_validate_json(call_row.content or "{}")
                persisted_result_payload = InternalMessage.model_validate_json(result_row.content or "{}")
                assert persisted_call_payload.tool_calls and persisted_call_payload.tool_calls[0].id == source["call_id"]
                assert persisted_result_payload.tool_call_id == source["call_id"]
                assert json.loads(persisted_result_payload.content or "{}")["status"] == "queued"

            marker_rows = [message for message in messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(marker_rows) == 2
            marker_payloads = [json.loads(message.content or "{}") for message in marker_rows]
            assert {payload["task"]["stdout"] for payload in marker_payloads} == {"result-A", "result-B"}
            assert all(payload["ui_only"] is True for payload in marker_payloads)
            for task in tasks:
                owner = task.extra["reply_owner"]
                assert owner["status"] == "handled" and owner["work_id"] == target_id and owner["identity"] == target_work.dedupe_key
            task_ids = [task.id for task in tasks]
            marker_ids = [message.id for message in marker_rows]

        assert len(sent_events) == 1
        assert sent_events[0][0:2] == (UID, SESSION_ID)
        assert sent_events[0][2]["work_id"] == target_id

        for task_id in task_ids:
            assert await external_work_module.resume_external_work_for_task(task_id) is False

        async with factory() as db:
            after_works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            after_markers = list((await db.execute(select(Message).where(Message.type == MessageType.BACKGROUND_TASK_RESULT))).scalars().all())
            assert len(after_works) == 3 and len(after_markers) == len(marker_ids) == 2
            assert {work.status for work in after_works} == {SessionReplyWorkStatus.MERGED, SessionReplyWorkStatus.SUCCEEDED}
            assert len(captured_requests) == 1
