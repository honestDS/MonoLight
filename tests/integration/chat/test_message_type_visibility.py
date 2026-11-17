import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import AGENT_WORK_TOOL_NAME, SEND_FILE_TO_USER_TOOL_NAME
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.utils.dispatcher.helpers import dump_background_proactive_history, dump_output_history
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkType

SESSION_ID = "message-type-visibility-session"
UID = "message-type-visibility-user"
PROFILE_ID = 1


@pytest_asyncio.fixture
async def message_type_session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add(ChatSession(session_id=SESSION_ID, uid=UID))
            await db.commit()
        yield session_factory


def _background_payload(*, task_id: int, tool_name: str) -> dict[str, Any]:
    task = {
        "status": "succeeded",
        "tool_name": tool_name,
        "files": [{"id": "generated-file", "name": "generated.png"}],
    }
    return {
        "type": "background_tool_result",
        "ui_only": True,
        "task_id": task_id,
        "tool_name": tool_name,
        "execution_status": "succeeded",
        "instruction": "Resume the completed background task.",
        "task": task,
        "tool_call": {
            "id": "background-tool-call",
            "name": tool_name,
            "arguments": {"path": "generated.png"},
        },
        "tool_result": {
            "tool_call_id": "background-tool-call",
            "content": json.dumps(task, ensure_ascii=False),
        },
        "original_tool_result_message_id": 101,
        "request_context": [{"role": "user", "content": "完成后台图片任务"}],
        "source": "background_task",
        "purpose": "deliver generated image",
    }


def _subagent_resume_content() -> str:
    return json.dumps(
        {
            "type": "external_work_resume",
            "source": "subagent",
            "instruction": "Continue the parent task with the completed subagent result.",
            "tool_call": {
                "id": "agent-work-call",
                "name": AGENT_WORK_TOOL_NAME,
                "arguments": {"instruction": "inspect the result"},
            },
            "results": [{"status": "succeeded", "content": "subagent completed"}],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _message_case(message_type: MessageType) -> tuple[MessageRole, Any]:
    if message_type == MessageType.TEXT:
        return (
            MessageRole.USER,
            InternalMessage(
                role=MessageRole.USER,
                content=json.dumps({"type": "subagent_result", "content": "user text"}, ensure_ascii=False),
            ),
        )
    if message_type == MessageType.TOOL_CALL:
        return (
            MessageRole.ASSISTANT,
            InternalMessage(
                role=MessageRole.ASSISTANT,
                content="before tool",
                tool_calls=[
                    InternalToolCall(
                        id="message-type-call",
                        name=SEND_FILE_TO_USER_TOOL_NAME,
                        arguments={"path": "report.txt"},
                    )
                ],
            ),
        )
    if message_type == MessageType.TOOL_RESULT:
        return (
            MessageRole.TOOL,
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="message-type-call",
                content="tool result body",
                tool_execution_status="succeeded",
            ),
        )
    if message_type == MessageType.AUDIT_CONFIRMATION:
        return MessageRole.ASSISTANT, {"audit_record_id": 7, "summary": "confirm message type"}
    if message_type == MessageType.AUDIT_DECISION:
        return MessageRole.USER, "approve"
    if message_type == MessageType.BACKGROUND_TASK_RESULT:
        payload = _background_payload(task_id=42, tool_name=SEND_FILE_TO_USER_TOOL_NAME)
        return MessageRole.USER, InternalMessage(role=MessageRole.USER, content=json.dumps(payload, ensure_ascii=False))
    if message_type == MessageType.SUBAGENT_RESULT:
        return MessageRole.USER, InternalMessage(role=MessageRole.USER, content=_subagent_resume_content())
    if message_type == MessageType.GUIDANCE:
        return MessageRole.SYSTEM, "guidance body"
    if message_type == MessageType.SCHEDULED_TASK_TRIGGER:
        return MessageRole.USER, "scheduled task instruction"
    if message_type == MessageType.OUTBOUND_TEXT_REFINEMENT:
        return MessageRole.USER, InternalMessage(role=MessageRole.USER, content="refinement prompt")
    raise AssertionError(f"unhandled message type: {message_type}")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", tuple(MessageType), ids=lambda message_type: message_type.value)
async def test_message_type_survives_save_parse_checkpoint_and_frozen_restore(
    message_type: MessageType,
    message_type_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    role, content = _message_case(message_type)
    async with message_type_session_factory() as db:
        saved = await save_message(
            db,
            SESSION_ID,
            UID,
            role,
            message_type,
            content,
            PROFILE_ID,
        )
        assert saved.id is not None
        assert saved.type == message_type

        persisted = await db.get(Message, saved.id)
        assert persisted is not None
        assert persisted.type == message_type
        parsed = parse_db_messages_to_internal([persisted])

        if message_type in {MessageType.AUDIT_CONFIRMATION, MessageType.AUDIT_DECISION}:
            assert parsed == []
            if message_type == MessageType.AUDIT_CONFIRMATION:
                assert json.loads(persisted.content or "{}") == content
            else:
                assert persisted.content == content
        else:
            assert len(parsed) == 1
            parsed_message = parsed[0]
            assert parsed_message.type == message_type
            assert parsed_message.role == role
            if message_type == MessageType.TEXT:
                assert parsed_message.content == content.content
                assert persisted.content == content.content
            elif message_type == MessageType.TOOL_CALL:
                assert parsed_message.content == "before tool"
                assert parsed_message.tool_calls is not None
                assert parsed_message.tool_calls[0].id == "message-type-call"
                assert parsed_message.tool_calls[0].arguments == {"path": "report.txt"}
            elif message_type == MessageType.TOOL_RESULT:
                assert parsed_message.content == "tool result body"
                assert parsed_message.tool_call_id == "message-type-call"
                assert parsed_message.tool_execution_status == "succeeded"
            elif message_type == MessageType.BACKGROUND_TASK_RESULT:
                parsed_payload = json.loads(parsed_message.content or "{}")
                assert parsed_payload["type"] == "external_work_resume"
                assert parsed_payload["tasks"][0]["task_id"] == 42
                assert parsed_payload["tasks"][0]["task"]["tool_name"] == SEND_FILE_TO_USER_TOOL_NAME
            elif message_type == MessageType.SUBAGENT_RESULT:
                assert parsed_message.content == content.content
            elif message_type == MessageType.GUIDANCE:
                assert parsed_message.content == content
            elif message_type == MessageType.SCHEDULED_TASK_TRIGGER:
                assert parsed_message.content == content
                assert parsed_message.execution_guidance == SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
            elif message_type == MessageType.OUTBOUND_TEXT_REFINEMENT:
                assert parsed_message.content == content.content

        serialized_messages = [message.model_dump(mode="json", exclude_none=True) for message in parsed]
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=f"message-type:{message_type.value}",
            dedupe_key=f"message-type:{message_type.value}",
            commit=False,
        )
        assert created is True
        assert work.id is not None
        checkpoint = {
            "messages": serialized_messages,
            "turn_messages": [dict(message) for message in serialized_messages],
            "message_type": message_type.value,
        }
        await session_reply_work_item_crud.save_checkpoint(db, work.id, checkpoint)
        frozen_ids = await session_reply_work_item_crud.save_frozen_context(db, work.id, serialized_messages)
        work.execution_state = {"frozen_context_message_ids": frozen_ids}
        db.add(work)
        await db.commit()

        restored_work = await session_reply_work_item_crud.get(db, work.id)
        assert restored_work is not None
        restored_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work.id)
        restored_frozen = await session_reply_work_item_crud.get_frozen_context(db, restored_work)

    assert restored_checkpoint == checkpoint
    assert restored_frozen == serialized_messages
    restored_messages = [InternalMessage.model_validate(message) for message in restored_checkpoint["messages"]]
    assert [message.type for message in restored_messages] == [message_type] * len(parsed)
    assert restored_messages == parsed
    if message_type == MessageType.TOOL_RESULT:
        assert restored_messages[0].tool_call_id == "message-type-call"
    if message_type == MessageType.TOOL_CALL:
        assert restored_messages[0].tool_calls is not None
        assert restored_messages[0].tool_calls[0].id == "message-type-call"


def _persisted_message(
    message_id: int,
    *,
    role: MessageRole,
    message_type: MessageType,
    content: str,
    created_at: datetime,
) -> Message:
    return Message(
        id=message_id,
        session_id=SESSION_ID,
        uid=UID,
        profile_id=PROFILE_ID,
        role=role,
        type=message_type,
        content=content,
        is_processed=True,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_subagent_result_is_hidden_only_from_public_pagination(message_type_session_factory) -> None:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    user_text = json.dumps({"type": "subagent_result", "content": "still user text"}, ensure_ascii=False)
    rows = [
        _persisted_message(
            1,
            role=MessageRole.USER,
            message_type=MessageType.TEXT,
            content=user_text,
            created_at=base_time,
        ),
        _persisted_message(
            2,
            role=MessageRole.ASSISTANT,
            message_type=MessageType.TEXT,
            content="<conversation_summary>normal summary</conversation_summary>",
            created_at=base_time + timedelta(seconds=1),
        ),
        _persisted_message(
            3,
            role=MessageRole.USER,
            message_type=MessageType.SUBAGENT_RESULT,
            content=_subagent_resume_content(),
            created_at=base_time + timedelta(seconds=2),
        ),
    ]
    async with message_type_session_factory() as db:
        db.add_all(rows)
        await db.commit()

        page = await message_crud.get_history_paged(
            db,
            session_id=SESSION_ID,
            uid=UID,
            limit=2,
        )
        incremental = await message_crud.get_history_paged(
            db,
            session_id=SESSION_ID,
            uid=UID,
            after_id=0,
            limit=2,
        )
        internal_history = await message_crud.get_history(
            db,
            session_id=SESSION_ID,
            uid=UID,
            limit=10,
        )
        internal_forward = await message_crud.get_history_forward_by_id(
            db,
            session_id=SESSION_ID,
            uid=UID,
            limit=10,
        )

    assert [message.id for message in page] == [2, 1]
    assert [message.id for message in incremental] == [1, 2]
    assert [message.id for message in internal_history] == [3, 2, 1]
    assert [message.id for message in internal_forward] == [1, 2, 3]
    assert any(message.type == MessageType.SUBAGENT_RESULT for message in internal_history)
    assert any(message.type == MessageType.SUBAGENT_RESULT for message in internal_forward)


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False], ids=["show-tools", "hide-tools"])
async def test_output_history_hides_subagent_result_without_hiding_other_message_types(
    message_type_session_factory,
    show_tool_calls: bool,
) -> None:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    user_text = json.dumps({"type": "subagent_result", "content": "still user text"}, ensure_ascii=False)
    tool_call = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="output-call", name="generate_image", arguments={"prompt": "a cat"})],
    )
    tool_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="output-call", content="image result")
    background_payload = _background_payload(task_id=43, tool_name="generate_image")
    rows = [
        _persisted_message(1, role=MessageRole.USER, message_type=MessageType.TEXT, content=user_text, created_at=base_time),
        _persisted_message(2, role=MessageRole.ASSISTANT, message_type=MessageType.TOOL_CALL, content=tool_call.model_dump_json(exclude_none=True), created_at=base_time + timedelta(seconds=1)),
        _persisted_message(3, role=MessageRole.TOOL, message_type=MessageType.TOOL_RESULT, content=tool_result.model_dump_json(exclude_none=True), created_at=base_time + timedelta(seconds=2)),
        _persisted_message(4, role=MessageRole.USER, message_type=MessageType.SUBAGENT_RESULT, content=_subagent_resume_content(), created_at=base_time + timedelta(seconds=3)),
        _persisted_message(5, role=MessageRole.ASSISTANT, message_type=MessageType.TEXT, content="<conversation_summary>normal summary</conversation_summary>", created_at=base_time + timedelta(seconds=4)),
        _persisted_message(6, role=MessageRole.USER, message_type=MessageType.BACKGROUND_TASK_RESULT, content=json.dumps(background_payload, ensure_ascii=False), created_at=base_time + timedelta(seconds=5)),
        _persisted_message(7, role=MessageRole.SYSTEM, message_type=MessageType.GUIDANCE, content="guidance output", created_at=base_time + timedelta(seconds=6)),
    ]
    async with message_type_session_factory() as db:
        db.add_all(rows)
        await db.commit()
        persisted = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())

    parsed = parse_db_messages_to_internal(persisted)
    output = dump_output_history(parsed, show_tool_calls=show_tool_calls)
    proactive_output = dump_background_proactive_history(parsed)

    expected_output_ids = [1, 5, 6, 7] if not show_tool_calls else [1, 2, 3, 5, 6, 7]
    assert [message["id"] for message in output] == expected_output_ids
    assert [message["id"] for message in proactive_output] == [1, 5, 6, 7]
    output_by_id = {message["id"]: message for message in output}
    assert output_by_id[1]["type"] == MessageType.TEXT
    assert output_by_id[1]["content"] == user_text
    assert output_by_id[5]["content"] == "<conversation_summary>normal summary</conversation_summary>"
    assert output_by_id[7]["content"] == "guidance output"
    background_output = json.loads(output_by_id[6]["content"])
    assert background_output["type"] == "external_work_resume"
    assert background_output["tasks"][0]["task"]["tool_name"] == "generate_image"
