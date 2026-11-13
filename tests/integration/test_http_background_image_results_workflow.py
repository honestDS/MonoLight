import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel, select

from app.api.v1.chat import router
from app.core.constants import (
    END_SESSION_TOOL_NAME,
    ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED,
)
from app.core.i18n import t
from app.core.prompts import (
    GOAL_MODE_FINAL_RESPONSE_PROMPT,
    GOAL_MODE_SYSTEM_PROMPT,
    TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT,
)
from app.core.security import get_current_user
from app.core.tools.send_file_to_user import resolve_file_token
from app.handler import register_handlers
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.database import get_db
from tests.unit.response_test_support import llm_response_payload

UID = "http-image-user"
SESSION_ID = "http-image-session"
MODEL_ID = "http-image-model"


async def _create_registered_tables(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    await connection.run_sync(SQLModel.metadata.create_all)


async def _seed_reply_work(
    db_session: AsyncSession,
    *,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> tuple[Profile, ChatSession, SessionReplyWorkItem, BackgroundTask]:
    profile = Profile(uid=UID, name="http image profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None

    session = ChatSession(
        session_id=SESSION_ID,
        uid=UID,
        profile_id=profile.id,
        source="http",
        show_tool_calls=False,
    )
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="http-image-foreground-work",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={"response": llm_response_payload("已提交生图", history=[])},
    )
    task = BackgroundTask(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        tool_call_id="generate-image-call",
        tool_name="generate_image",
        status=task_status,
        arguments={"prompt": "test apple"},
        auto_reply=True,
        reply_status=reply_status,
    )
    db_session.add_all([session, work, task])
    await db_session.commit()
    await db_session.refresh(work)
    await db_session.refresh(task)
    assert work.id is not None
    assert task.id is not None
    return profile, session, work, task


def _build_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return SimpleNamespace(uid=UID)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "reply_status"),
    [
        (BackgroundTaskStatus.PENDING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.RUNNING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.SUCCEEDED, BackgroundTaskReplyStatus.RUNNING),
    ],
)
async def test_http_reply_work_status_exposes_hidden_background_image_activity(
    db_session: AsyncSession,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> None:
    await _create_registered_tables(db_session)
    _profile, _session, work, _task = await _seed_reply_work(
        db_session,
        task_status=task_status,
        reply_status=reply_status,
    )
    app = _build_app(db_session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/chat/reply-works/{work.id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert data["response"]["history"] == []
    assert data["response"]["has_background_tasks"] is True
    assert data["response"]["background_task_poll_interval"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.parametrize("final_tool_violations", [0, 1, 3, 4])
async def test_completed_background_image_message_is_returned_by_http_history(
    image_chat_runtime,
    protocol: str,
    final_tool_violations: int,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=True) as runtime:
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="generate-image-call",
                        name="generate_image",
                        arguments={"prompt": "test apple"},
                    )
                ],
            )
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": SESSION_ID,
                "request_id": "image-delivery-request",
                "message": "画一个苹果并发给我",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        work_id = accepted_data["work_id"]
        assert isinstance(work_id, int)

        assert (await runtime.run_next()).status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        waiting_reply = await runtime.client.get(f"/api/v1/chat/reply-works/{work_id}")
        assert waiting_reply.status_code == 200
        waiting_data = waiting_reply.json()["data"]
        assert waiting_data["work_id"] == work_id and waiting_data["status"] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value
        assert waiting_data["response"] is None

        async with runtime.factory() as db:
            works = (await db.execute(select(SessionReplyWorkItem))).scalars().all()
            tasks = (await db.execute(select(BackgroundTask))).scalars().all()
            assert len(works) == len(tasks) == 1
            assert works[0].id == work_id and works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
            task = tasks[0]
            assert task.id is not None
            task_id = task.id
            assert task.tool_name == "generate_image" and task.arguments == {"prompt": "test apple"}
            assert task.status == BackgroundTaskStatus.PENDING and task.auto_reply is False
            assert task.extra.get("origin_work_id") == work_id and task.result is None

        initial_history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": SESSION_ID})
        assert initial_history_response.status_code == 200
        initial_history = initial_history_response.json()["data"]
        assert "assistant_files" not in json.dumps(initial_history, ensure_ascii=False)
        assert runtime.image_requests == []

        await runtime.complete_images()
        async with runtime.factory() as db:
            completed_task = await db.get(BackgroundTask, task_id)
            assert completed_task is not None and completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.result is not None
            task_result = json.loads(json.dumps(completed_task.result, ensure_ascii=False))
            generated_path = Path(task_result["content"]["send_file_to_user"]["files"][0]["path"])
        assert generated_path.is_file() and generated_path.read_bytes() == runtime.image_bytes

        send_file_path = str(generated_path)
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="send-file-call", name="send_file_to_user", arguments={"files": [{"path": send_file_path}]})],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="end-session-call", name=END_SESSION_TOOL_NAME, arguments={})],
                ),
            ]
        )
        for violation_index in range(final_tool_violations):
            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="伪成功正文",
                    tool_calls=[
                        InternalToolCall(id=f"final-violation-{violation_index + 1}-send-file-call", name="send_file_to_user", arguments={"files": [{"path": send_file_path}]}),
                        InternalToolCall(id=f"final-violation-{violation_index + 1}-generate-image-call", name="generate_image", arguments={"prompt": "do not generate"}),
                    ],
                )
            )
        if final_tool_violations <= 3:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="后台图片已发送"))

        resumed = await runtime.run_next()
        assert resumed is not None and resumed.id == work_id

        async def fetch_http_state() -> tuple[dict, list[dict], dict]:
            reply = await runtime.client.get(f"/api/v1/chat/reply-works/{work_id}")
            history = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": SESSION_ID})
            pending = await runtime.client.get("/api/v1/chat/background-tasks/pending-activity", params={"session_id": SESSION_ID})
            assert all(response.status_code == 200 for response in (reply, history, pending))
            return reply.json()["data"], history.json()["data"], pending.json()["data"]

        def assistant_file_items(history: list[dict]) -> list[tuple[dict, dict]]:
            result = []
            for item in history:
                if item.get("role") != MessageRole.ASSISTANT.value:
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                if isinstance(content, dict) and content.get("type") == "assistant_files":
                    result.append((item, content))
            return result

        def assert_no_internal_prompts(value: object) -> None:
            serialized = json.dumps(value, ensure_ascii=False)
            for prompt in (GOAL_MODE_SYSTEM_PROMPT, GOAL_MODE_FINAL_RESPONSE_PROMPT, TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT):
                assert prompt not in serialized

        work_data, history, pending = await fetch_http_state()
        assert pending == {"has_pending_activity": False}
        assert_no_internal_prompts(history)

        async def load_db_state() -> tuple[list[SessionReplyWorkItem], list[BackgroundTask], list[Message], list[SessionEvent]]:
            async with runtime.factory() as db:
                works = (await db.execute(select(SessionReplyWorkItem))).scalars().all()
                tasks = (await db.execute(select(BackgroundTask))).scalars().all()
                messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all()
                events = (await db.execute(select(SessionEvent).where(SessionEvent.uid == UID, SessionEvent.session_id == SESSION_ID).order_by(SessionEvent.id))).scalars().all()
            return works, tasks, messages, events

        if final_tool_violations == 4:
            expected_error = t(ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED)
            assert work_data["status"] == SessionReplyWorkStatus.FAILED.value
            assert work_data["error"] == expected_error
            assert any(item.get("content") == expected_error for item in history)
            assert not assistant_file_items(history)
            assert "伪成功正文" not in json.dumps(history, ensure_ascii=False)

            works, tasks, messages, events = await load_db_state()
            assert len(works) == 1
            assert works[0].id == work_id and works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
            assert len(tasks) == 1
            assert tasks[0].id == task_id and tasks[0].status == BackgroundTaskStatus.SUCCEEDED
            assert tasks[0].result == task_result
            persisted_dump = json.dumps([message.content for message in messages], ensure_ascii=False)
            assert not any(marker in persisted_dump for marker in ("final-violation-", "伪成功正文", "assistant_files"))
            error_messages = [message for message in messages if message.role == MessageRole.ERR]
            assert len(error_messages) == 1 and error_messages[0].content == expected_error

            error_events = [row for row in events if row.event.get("type") == "proactive_reply_error"]
            assert len(error_events) == 1
            error_event_row = error_events[0]
            error_event = error_event_row.event
            assert (error_event_row.uid, error_event_row.session_id, error_event["session_id"], error_event["work_id"]) == (UID, SESSION_ID, SESSION_ID, work_id)
            assert error_event["message_id"] == work_data["result_message_id"]
            assert error_event["content"] == expected_error and error_event["files"] == []
            event_snapshot = [(row.id, row.dedupe_key, row.event) for row in events]
            assert await runtime.run_next() is None
            _works, _tasks, _messages, repeated_events = await load_db_state()
            assert [(row.id, row.dedupe_key, row.event) for row in repeated_events] == event_snapshot
            assert len(runtime.image_requests) == 1
            return

        assert work_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert work_data["work_id"] == work_id
        response = work_data["response"]
        response_content = json.loads(response["choices"][0]["message"]["content"])
        assert response_content["type"] == "assistant_files" and response_content["text"] == "后台图片已发送"
        assert response_content["files"] == response["files"]
        assert len(response["files"]) == 1 and response["files"][0]["mime_type"] == "image/png"
        response_file = response["files"][0]

        assistant_items = assistant_file_items(history)
        assert len(assistant_items) == 1
        history_item, assistant_files = assistant_items[0]
        assert assistant_files["text"] == "后台图片已发送"
        assert len(assistant_files["files"]) == 1
        history_file = assistant_files["files"][0]
        assert history_file["id"] == response_file["id"]
        assert history_file["download_url"] == response_file["download_url"]
        assert history_item["id"] == response["message_id"]
        download = await runtime.client.get(history_file["download_url"])
        assert download.status_code == 200
        assert download.content == runtime.image_bytes

        repeated_work, repeated_history, repeated_pending = await fetch_http_state()
        assert repeated_pending == {"has_pending_activity": False}
        assert (repeated_work["status"], repeated_work["work_id"]) == (work_data["status"], work_data["work_id"])
        repeated_content = json.loads(repeated_work["response"]["choices"][0]["message"]["content"])
        assert repeated_content["type"] == "assistant_files" and repeated_content["text"] == "后台图片已发送"
        assert repeated_content["files"] == repeated_work["response"]["files"] == response["files"]
        repeated_assistant_items = assistant_file_items(repeated_history)
        assert len(repeated_assistant_items) == 1
        assert repeated_assistant_items[0][1]["files"] == assistant_files["files"]
        assert_no_internal_prompts(repeated_history)

        works, tasks, messages, events = await load_db_state()
        assert len(works) == 1 and works[0].id == work_id and works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
        assert works[0].status == SessionReplyWorkStatus.SUCCEEDED
        assert len(tasks) == 1
        assert tasks[0].id == task_id and tasks[0].status == BackgroundTaskStatus.SUCCEEDED
        assert tasks[0].result == task_result
        persisted_dump = json.dumps([message.content for message in messages], ensure_ascii=False)
        assert "final-violation-" not in persisted_dump and "伪成功正文" not in persisted_dump

        proactive_events = [row for row in events if row.event.get("type") == "proactive_reply"]
        assert len(proactive_events) == 1
        event_row = proactive_events[0]
        event = event_row.event
        assert (event_row.uid, event_row.session_id, event["session_id"], event["work_id"]) == (UID, SESSION_ID, SESSION_ID, work_id)
        assert event["message_id"] == response["message_id"]
        assert event["content"] == "后台图片已发送" and event["files"] == response["files"]
        assert_no_internal_prompts(event)

        event_snapshot = [(row.id, row.dedupe_key, row.event) for row in events]
        assert await runtime.run_next() is None
        _works, _tasks, _messages, repeated_events = await load_db_state()
        assert [(row.id, row.dedupe_key, row.event) for row in repeated_events] == event_snapshot
        assert len(runtime.image_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "requested_count",
        "max_parallel_tools",
        "target_index",
        "sibling_indexes",
        "sibling_result_kind",
        "history_id_reuse",
    ),
    [
        (1, 5, 0, (), "none", "none"),
        (5, 5, 2, (), "none", "none"),
        (5, 5, 2, (0,), "queued", "none"),
        (8, 5, 2, (), "none", "none"),
        (8, 5, 2, (0, 4), "queued", "none"),
        (8, 5, 2, (4, 0), "queued", "none"),
        (5, 5, 2, (0,), "file_success", "none"),
        (5, 5, 2, (0,), "queued", "sibling"),
        (5, 5, 2, (0,), "queued", "target"),
    ],
)
async def test_background_image_results_resume_real_chat_with_paired_tools(
    image_chat_runtime,
    tmp_path: Path,
    requested_count: int,
    max_parallel_tools: int,
    target_index: int,
    sibling_indexes: tuple[int, ...],
    sibling_result_kind: str,
    history_id_reuse: str,
) -> None:
    history_path = tmp_path / "history.txt"
    report_path = tmp_path / "report.txt"
    history_marker = "historical-file-marker"
    report_marker = "current-report-marker"
    history_path.write_text(f"{history_marker}\n", encoding="utf-8")
    report_path.write_text(f"{report_marker}\n", encoding="utf-8")
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False, max_parallel_tools=max_parallel_tools) as runtime:

        async def rows() -> tuple[list[SessionReplyWorkItem], list[BackgroundTask]]:
            async with runtime.factory() as db:
                works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
                tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            return works, tasks

        def assistant_file_items(history: list[dict]) -> list[tuple[dict, dict]]:
            items = []
            for message in history:
                if message.get("role") != MessageRole.ASSISTANT.value:
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                if isinstance(content, dict) and content.get("type") == "assistant_files":
                    items.append((message, content))
            return items

        def function_pairs(payload: dict) -> list[tuple[dict, dict]]:
            pending: list[dict] = []
            pairs: list[tuple[dict, dict]] = []
            items = payload.get("input")
            assert isinstance(items, list)
            for item in items:
                if not isinstance(item, dict) or item.get("type") not in {"function_call", "function_call_output"}:
                    continue
                if item.get("type") == "function_call":
                    pending.append(item)
                    continue
                call_id = item.get("call_id") or item.get("id")
                assert isinstance(call_id, str)
                match_index = next((index for index, call in enumerate(pending) if (call.get("call_id") or call.get("id")) == call_id), None)
                assert match_index is not None
                call = pending.pop(match_index)
                assert (call.get("call_id") or call.get("id")) == call_id
                pairs.append((call, item))
            assert not pending
            assert len(pairs) == sum(item.get("type") == "function_call" for item in items)
            assert len(pairs) == sum(item.get("type") == "function_call_output" for item in items)
            return pairs

        def output_payload(item: dict) -> tuple[str, dict]:
            assert isinstance(output := item.get("output"), str) and isinstance(parsed := json.loads(output), dict)
            assert isinstance(result := parsed["tool_result"], dict)
            return output, result

        history_calls = [InternalToolCall(id="history-image-call", name="generate_image", arguments={"prompt": "history image"}), InternalToolCall(id="history-file-call", name="file_tool", arguments={"operation": "read", "path": str(history_path.resolve())})]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=history_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={"session_id": SESSION_ID, "request_id": "history-request", "message": "此前用户文本"},
        )
        assert accepted.status_code == 200 and (history_work_id := accepted.json()["data"]["work_id"])
        history_waiting = await runtime.run_next()
        assert history_waiting is not None and history_waiting.id == history_work_id and history_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        _works, tasks = await rows()
        history_tasks = [task for task in tasks if task.extra.get("origin_work_id") == history_work_id]
        assert len(history_tasks) == 1
        history_task = history_tasks[0]
        assert history_task.tool_name == "generate_image" and history_task.arguments == {"prompt": "history image"} and history_task.status == BackgroundTaskStatus.PENDING and history_task.auto_reply is False and history_task.id is not None
        await runtime.complete_images(history_task.id)
        _works, tasks = await rows()
        history_task = next(task for task in tasks if task.id == history_task.id)
        assert history_task.status == BackgroundTaskStatus.SUCCEEDED and history_task.result is not None
        history_result = json.loads(json.dumps(history_task.result, ensure_ascii=False))
        history_image_path = Path(history_result["content"]["send_file_to_user"]["files"][0]["path"])
        assert history_image_path.is_file() and history_image_path.read_bytes() == runtime.image_bytes
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="history-send-files", name="send_file_to_user", arguments={"files": [{"path": str(history_image_path)}]})],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="此前助手文本"),
            ]
        )
        history_done = await runtime.run_next()
        assert history_done is not None and history_done.id == history_work_id and history_done.status == SessionReplyWorkStatus.SUCCEEDED
        history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": SESSION_ID})
        history_items = history_response.json()["data"]
        historical_messages = {item["id"]: item.get("content") for item in history_items if item.get("id") is not None}
        assert all(text in json.dumps(history_items, ensure_ascii=False) for text in ("此前用户文本", "此前助手文本"))
        historical_files = assistant_file_items(history_items)
        assert len(historical_files) == 1
        historical_file_entry = historical_files[0][1]["files"][0]
        historical_download = await runtime.client.get(historical_file_entry["download_url"])
        assert historical_download.status_code == 200 and historical_download.content == runtime.image_bytes
        current_calls = []
        for index in range(requested_count):
            if sibling_result_kind == "file_success" and index == 0:
                call = InternalToolCall(
                    id=f"image-call-{index + 1}",
                    name="file_tool",
                    arguments={"operation": "read", "path": str(report_path.resolve())},
                )
            else:
                call = InternalToolCall(id=f"image-call-{index + 1}", name="generate_image", arguments={"prompt": f"current image {index + 1}"})
            current_calls.append(call)
        if history_id_reuse == "sibling":
            current_calls[0].id = history_calls[1].id
        elif history_id_reuse == "target":
            current_calls[target_index].id = history_calls[0].id
        target_call = current_calls[target_index]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=current_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={"session_id": SESSION_ID, "request_id": "current-request", "message": "当前用户文本"},
        )
        assert accepted.status_code == 200 and (current_work_id := accepted.json()["data"]["work_id"])
        current_waiting = await runtime.run_next()
        assert current_waiting is not None and current_waiting.id == current_work_id and current_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        _works, tasks = await rows()
        current_tasks = [task for task in tasks if task.extra.get("origin_work_id") == current_work_id]
        executable_indexes = [index for index, call in enumerate(current_calls[:max_parallel_tools]) if call.name == "generate_image"]
        assert len(current_tasks) == len(executable_indexes)
        task_by_prompt = {task.arguments["prompt"]: task for task in current_tasks}
        for index in executable_indexes:
            task = task_by_prompt[current_calls[index].arguments["prompt"]]
            assert task.tool_name == "generate_image" and task.arguments == current_calls[index].arguments and task.status == BackgroundTaskStatus.PENDING and task.auto_reply is False and task.extra["origin_work_id"] == current_work_id
        assert {task.arguments["prompt"] for task in current_tasks} == {call.arguments["prompt"] for call in current_calls[:max_parallel_tools] if call.name == "generate_image"} and not (
            {call.arguments["prompt"] for call in current_calls[max_parallel_tools:] if call.name == "generate_image"} & {task.arguments["prompt"] for task in current_tasks}
        )
        sibling_task_ids = [task_by_prompt[current_calls[index].arguments["prompt"]].id for index in sibling_indexes if current_calls[index].name == "generate_image"]
        if sibling_task_ids:
            await runtime.complete_images(sibling_task_ids)
        _works, tasks = await rows()
        task_by_id = {task.id: task for task in tasks}
        for task_id in sibling_task_ids:
            assert task_by_id[task_id].status == BackgroundTaskStatus.SUCCEEDED
        target_task = task_by_prompt[target_call.arguments["prompt"]]
        assert target_task.id is not None
        await runtime.complete_images(target_task.id)
        _works, tasks = await rows()
        task_by_prompt = {task.arguments["prompt"]: task for task in tasks if task.extra.get("origin_work_id") == current_work_id}
        for index in executable_indexes:
            task = task_by_prompt[current_calls[index].arguments["prompt"]]
            expected_status = BackgroundTaskStatus.SUCCEEDED if index in {*sibling_indexes, target_index} else BackgroundTaskStatus.PENDING
            assert task.status == expected_status
        await runtime.complete_images()
        _works, tasks = await rows()
        current_tasks = [task for task in tasks if task.extra.get("origin_work_id") == current_work_id]
        assert all(task.status == BackgroundTaskStatus.SUCCEEDED and task.result is not None for task in current_tasks)
        current_results = {task.arguments["prompt"]: json.loads(json.dumps(task.result, ensure_ascii=False)) for task in current_tasks}
        current_paths = [Path(current_results[current_calls[index].arguments["prompt"]]["content"]["send_file_to_user"]["files"][0]["path"]) for index in executable_indexes]
        assert all(path.is_file() and path.read_bytes() == runtime.image_bytes for path in current_paths)
        task_results_before_resume = {task.id: json.loads(json.dumps(task.result, ensure_ascii=False)) for task in tasks}
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="current-send-files", name="send_file_to_user", arguments={"files": [{"path": str(path)} for path in current_paths]})],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="当前图片生成完成"),
            ]
        )
        resume_start = len(runtime.provider_payloads)
        current_done = await runtime.run_next()
        assert current_done is not None and current_done.id == current_work_id and current_done.status == SessionReplyWorkStatus.SUCCEEDED
        resume_payloads = runtime.provider_payloads[resume_start:]
        assert resume_payloads
        expected_specs = {(call.name, json.dumps(call.arguments, sort_keys=True)) for call in [*history_calls, *current_calls]}
        current_task_ids = {task.id for task in current_tasks}
        for resume_payload in resume_payloads:
            pairs = function_pairs(resume_payload)
            actual_specs = {(pair[0]["name"], json.dumps(json.loads(pair[0]["arguments"]), sort_keys=True)) for pair in pairs if pair[0].get("name") in {"generate_image", "file_tool"}}
            assert sum(pair[0].get("name") in {"generate_image", "file_tool"} for pair in pairs) == len(history_calls) + len(current_calls) and actual_specs == expected_specs
            for call_item, result_item in pairs:
                call_name = call_item["name"]
                call_arguments = json.loads(call_item["arguments"])
                result_text, result = output_payload(result_item)
                assert result_item["call_id"] == call_item["call_id"]
                if call_name == "generate_image":
                    prompt = call_arguments["prompt"]
                    if prompt == "history image":
                        assert result["status"] == "queued" and result["task_id"] == history_task.id
                    else:
                        index = int(prompt.rsplit(" ", 1)[-1]) - 1
                        if index >= max_parallel_tools:
                            assert result["status"] == "failed" and result["executed"] is False and "task_id" not in result
                        else:
                            assert result["status"] == "queued" and result["task_id"] == task_by_prompt[prompt].id
                elif call_name == "file_tool":
                    marker = {str(history_path.resolve()): history_marker, str(report_path.resolve()): report_marker}[call_arguments["path"]]
                    assert marker in result_text
                elif call_name != "send_file_to_user":
                    raise AssertionError(f"unexpected resumed tool: {call_name}")
            external_resume_tasks = []
            for item in resume_payload.get("input", []):
                content = item.get("content")
                if item.get("role") != "user" or not isinstance(content, str):
                    continue
                try:
                    resume = json.loads(json.loads(content).get("user_message", ""))
                except (AttributeError, TypeError, json.JSONDecodeError):
                    continue
                if isinstance(resume, dict) and resume.get("type") == "external_work_resume" and isinstance(resume.get("tasks"), list):
                    external_resume_tasks.extend(task for task in resume["tasks"] if isinstance(task, dict))
            assert external_resume_tasks
            notification_ids = [task.get("task_id") for task in external_resume_tasks]
            assert all(isinstance(task_id, int) for task_id in notification_ids)
            assert set(notification_ids) == {history_task.id} | current_task_ids
            assert len(notification_ids) == len(set(notification_ids))
            history_notifications = [task for task in external_resume_tasks if task.get("task_id") == history_task.id]
            current_notifications = [task for task in external_resume_tasks if task.get("task_id") in current_task_ids]
            assert len(history_notifications) == 1
            history_notification = history_notifications[0]
            assert history_notification["tool_call"] == {"id": history_task.tool_call_id, "name": history_task.tool_name, "arguments": history_task.arguments}
            assert history_notification["task"]["send_file_to_user"]["files"][0]["path"] == str(history_image_path)
            assert history_notification["request_context"] == history_task.extra.get("request_context")
            assert len(current_notifications) == len(current_task_ids)
            assert {task["task_id"] for task in current_notifications} == current_task_ids
            for notification in current_notifications:
                task = next(task for task in current_tasks if task.id == notification["task_id"])
                task_content = task.result["content"]
                assert notification["tool_call"] == {"id": task.tool_call_id, "name": task.tool_name, "arguments": task.arguments}
                assert notification["task"]["send_file_to_user"]["files"][0]["path"] == task_content["send_file_to_user"]["files"][0]["path"]
                assert notification["request_context"] == task.extra.get("request_context")
            resume_dump = json.dumps(resume_payload, ensure_ascii=False)
            required_markers = ("此前用户文本", "此前助手文本", "当前用户文本", history_marker) + ((report_marker,) if sibling_result_kind == "file_success" else ())
            assert all(text in resume_dump for text in required_markers)
            assert base64.b64encode(runtime.image_bytes).decode("ascii") not in resume_dump
        reply = await runtime.client.get(f"/api/v1/chat/reply-works/{current_work_id}")
        reply_data = reply.json()["data"]
        assert reply_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value and reply_data["work_id"] == current_work_id
        response = reply_data["response"]
        response_content = json.loads(response["choices"][0]["message"]["content"])
        assert response_content["type"] == "assistant_files" and response_content["text"] == "当前图片生成完成" and response_content["files"] == response["files"]
        assert len(response["files"]) == len(current_paths)
        delivered_paths = {resolve_file_token(file_entry["id"], uid=runtime.uid).resolve() for file_entry in response["files"]}
        assert delivered_paths == {path.resolve() for path in current_paths}
        assert history_image_path.resolve() not in delivered_paths
        assert historical_file_entry["id"] not in {file_entry["id"] for file_entry in response["files"]}
        for file_entry in response["files"]:
            download = await runtime.client.get(file_entry["download_url"])
            assert download.status_code == 200 and download.content == runtime.image_bytes
        historical_download = await runtime.client.get(historical_file_entry["download_url"])
        assert historical_download.status_code == 200 and historical_download.content == runtime.image_bytes
        current_history_response, repeated_history_response = await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": SESSION_ID}), await runtime.client.get("/api/v1/chat/sessions/history", params={"session_id": SESSION_ID})
        current_history, repeated_history = current_history_response.json()["data"], repeated_history_response.json()["data"]
        current_file_items, repeated_file_items = assistant_file_items(current_history), assistant_file_items(repeated_history)
        assert len(current_file_items) == len(repeated_file_items) == 2
        assert [(item[0]["id"], item[1]) for item in current_file_items] == [(item[0]["id"], item[1]) for item in repeated_file_items]
        repeated_by_id = {item.get("id"): item.get("content") for item in repeated_history}
        assert all(repeated_by_id[message_id] == content for message_id, content in historical_messages.items())
        works, tasks = await rows()
        assert {work.id for work in works} == {history_work_id, current_work_id}
        assert all(work.work_type == SessionReplyWorkType.FOREGROUND_REPLY and work.status == SessionReplyWorkStatus.SUCCEEDED for work in works)
        assert all(task.status == BackgroundTaskStatus.SUCCEEDED and task.auto_reply is False for task in tasks)
        assert {task.id: json.loads(json.dumps(task.result, ensure_ascii=False)) for task in tasks} == task_results_before_resume
        assert len(tasks) == 1 + len(executable_indexes)
        image_request_count = len(runtime.image_requests)
        assert image_request_count == 1 + len(executable_indexes)
        assert await runtime.run_next() is None
        assert len(runtime.image_requests) == image_request_count
