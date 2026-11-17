import json
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.adapters.weixin_openclaw.message import extract_text_and_attachments
from app.core.constants import ERR_CHAT_REQUEST_ID_CONFLICT
from app.core.crud.session.message import message_crud
from app.core.exceptions import BaseBusinessException, ParameterException
from app.core.session_reply_queue import manager_result as manager_result_module
from app.core.session_reply_queue.manager import SessionReplyQueueManager, build_session_reply_work_event_id
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import (
    ChatCompletionRequest,
    FilePart,
    ImagePart,
    InternalMessage,
    Message,
    MessageContentFormat,
    MessageResponse,
    MessageRole,
    MessageType,
    TextPart,
    VoiceTextPart,
)
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.transformers.openai.chat_completions import OpenAIChatCompletionsTransformer
from app.transformers.openai.responses import OpenAIResponsesTransformer
from tests.integration.session_reply_queue_fixture import (
    db_session as _db_session,
)

db_session = _db_session


@pytest.mark.asyncio
async def test_external_foreground_message_uses_latest_guidance_across_turns(db_session: AsyncSession):
    manager = SessionReplyQueueManager()
    db_session.add(
        ChatSession(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            source="weixin-openclaw",
            reply_target_source="weixin-openclaw",
        )
    )
    guidance_messages = [
        Message(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content="[系统提示信息]第一条引导[系统提示信息结束]",
            is_processed=False,
        ),
        Message(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content="[系统提示信息]第二条引导[系统提示信息结束]",
            is_processed=False,
        ),
    ]
    db_session.add_all(guidance_messages)
    await db_session.commit()

    initial_message, work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="用户从 IM 发来的消息",
        attachments=None,
        source="weixin-openclaw",
    )
    input_message = await db_session.get(Message, initial_message.id)
    latest_guidance = guidance_messages[-1].content

    assert input_message.content == "用户从 IM 发来的消息"
    assert input_message.guidance_prompt == latest_guidance
    assert work.execution_state["guidance_prompt"] == latest_guidance
    assert "additional_system_prompt" not in work.execution_state
    for guidance in guidance_messages:
        await db_session.refresh(guidance)
        assert guidance.is_processed is True

    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = "worker-1"
    db_session.add(work)
    await db_session.commit()
    content, attachments, message_ids = await manager.freeze_foreground_input(
        db_session,
        work=work,
        worker_id="worker-1",
    )

    assert content == "用户从 IM 发来的消息"
    assert attachments == []
    assert message_ids == [input_message.id]

    work.status = SessionReplyWorkStatus.SUCCEEDED
    work.locked_by = None
    db_session.add(work)
    await db_session.commit()

    next_initial_message, next_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="第二条 IM 消息",
        attachments=None,
        source="weixin-openclaw",
    )
    next_input_message = await db_session.get(Message, next_initial_message.id)

    assert next_input_message.guidance_prompt == latest_guidance
    assert next_work.execution_state["guidance_prompt"] == latest_guidance

    latest_guidance = await message_crud.create_guidance(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        content="[系统提示信息]第三条引导[系统提示信息结束]",
    )
    newest_initial_message, newest_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="第三条 IM 消息",
        attachments=None,
        source="weixin-openclaw",
    )
    newest_input_message = await db_session.get(Message, newest_initial_message.id)

    assert newest_input_message.guidance_prompt == latest_guidance.content
    assert newest_work.execution_state["guidance_prompt"] == latest_guidance.content
    assert guidance_messages[0].content not in newest_input_message.guidance_prompt
    assert guidance_messages[1].content not in newest_input_message.guidance_prompt


@pytest.mark.asyncio
async def test_permanent_guidance_is_visible_in_web_history_but_excluded_from_model_history(db_session: AsyncSession):
    guidance = await message_crud.create_guidance(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        content="[系统提示信息]永久引导[系统提示信息结束]",
        commit=False,
    )
    text_message = Message(
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="普通消息",
        is_processed=False,
    )
    refinement_message = Message(
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.OUTBOUND_TEXT_REFINEMENT,
        content="[系统提示,此处不是用户说的话]上一条助手回复超过渠道文本限制。... [系统提示结束]",
        is_processed=False,
    )
    db_session.add_all([text_message, refinement_message])
    await db_session.commit()

    model_history = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
    )
    unprocessed = await message_crud.get_unprocessed_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
    )
    web_history = await message_crud.get_history_paged(
        db_session,
        session_id="session-1",
        uid="user-1",
    )

    assert [message.id for message in model_history] == [text_message.id]
    assert [message.id for message in unprocessed] == [text_message.id]
    assert {message.id for message in web_history} == {guidance.id, text_message.id}
    assert refinement_message.id not in {message.id for message in model_history}
    assert refinement_message.id not in {message.id for message in unprocessed}
    assert refinement_message.id not in {message.id for message in web_history}
    assert guidance.id in {message.id for message in web_history}
    assert text_message.id in {message.id for message in web_history}


@pytest.mark.asyncio
async def test_wait_for_result_returns_resolved_work_id(monkeypatch):
    manager = SessionReplyQueueManager()
    resolved_work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.SUCCEEDED,
        result_message_id=9,
        execution_state={
            "response": {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "result"},
                        "finish_reason": True,
                    }
                ]
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return resolved_work

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(manager_result_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)

    response = await manager.wait_for_result(9)

    assert response["work_id"] == 7
    assert response["work_sequence_no"] == 1
    assert response["message_id"] == 9
    assert response["choices"][0]["message"]["content"] == "result"


@pytest.mark.asyncio
async def test_wait_for_stream_returns_result_message_identity_in_done_event(monkeypatch):
    manager = SessionReplyQueueManager()
    resolved_work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_type=SessionReplySourceType.AUDIT_RECORD,
        source_id="42",
        dedupe_key="confirmed-audit:42",
        status=SessionReplyWorkStatus.SUCCEEDED,
        result_message_id=9,
        execution_state={
            "request_ids": ["request-1"],
            "response": {
                "content": "confirmed result",
                "history": [],
                "files": [],
                "response_id": "response-turn-final",
            },
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return resolved_work

    async def list_after_sequence(db, *, work_id, after_sequence_no):
        return []

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(manager_result_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)
    monkeypatch.setattr(manager_result_module.session_reply_stream_event_crud, "list_after_sequence", list_after_sequence)

    events = [event async for event in manager.wait_for_stream(7)]

    assert events == [
        {
            "type": "done",
            "session_id": "session-1",
            "work_id": 7,
            "work_sequence_no": 1,
            "response_id": "response-turn-final",
            "message_id": 9,
            "history": [],
            "files": [],
            "response": {
                "content": "confirmed result",
                "history": [],
                "files": [],
                "response_id": "response-turn-final",
                "work_id": 7,
                "work_sequence_no": 1,
                "message_id": 9,
            },
            "request_ids": ["request-1"],
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_result_restores_persisted_user_error_for_adapter(monkeypatch):
    manager = SessionReplyQueueManager()
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.FAILED,
        result_message_id=9,
        error="internal provider failure",
    )
    error_message = SimpleNamespace(content="所有对话渠道均不可用")

    class FakeSession:
        async def get(self, model, object_id):
            assert model is Message
            assert object_id == 9
            return error_message

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return work

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(manager_result_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)

    with pytest.raises(BaseBusinessException, match="所有对话渠道均不可用") as exc_info:
        await manager.wait_for_result(9)

    assert exc_info.value.data == {
        "work_id": 7,
        "event_id": build_session_reply_work_event_id(work, error=True),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws", "weixin-openclaw"])
async def test_foreground_freeze_preserves_voice_source_through_history_and_provider(
    db_session: AsyncSession,
    source: str,
):
    manager = SessionReplyQueueManager()
    request = ChatCompletionRequest(
        message=[{"type": "text", "text": "语音转写内容", "input_source": "voice"}],
        attachments=["virtual.png"],
    )
    first_message, first_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message=request.message,
        attachments=request.attachments,
        source=source,
    )
    second_message, second_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="普通文本",
        attachments=["virtual.pdf", "virtual.png"],
        source=source,
    )

    first_work.status = SessionReplyWorkStatus.RUNNING
    first_work.locked_by = "worker-1"
    db_session.add(first_work)
    await db_session.commit()

    first_result = await manager.freeze_foreground_input(db_session, work=first_work, worker_id="worker-1")
    await db_session.refresh(first_work)
    second_result = await manager.freeze_foreground_input(db_session, work=first_work, worker_id="worker-1")

    expected_message_ids = [first_message.id, second_message.id]
    assert first_result == second_result
    frozen_content, frozen_attachments, frozen_message_ids = first_result
    assert frozen_message_ids == expected_message_ids
    assert frozen_attachments == ["virtual.png", "virtual.pdf"]
    assert [part.model_dump(mode="json") for part in frozen_content] == [
        {"type": "text", "text": "语音转写内容", "input_source": "voice"},
        {"type": "text", "text": "普通文本"},
    ]
    assert first_work.input_message_ids == expected_message_ids
    await db_session.refresh(second_work)
    assert second_work.status == SessionReplyWorkStatus.MERGED
    assert second_work.merged_into_id == first_work.id

    for transformer in (OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer):
        model_message = InternalMessage(
            role=MessageRole.USER,
            content=frozen_content,
            attachments=frozen_attachments,
        )
        materialized_message = materialize_user_environment_prompts([model_message])[0]
        provider_message = transformer.to_provider([materialized_message])[0]
        provider_content = provider_message["content"]
        provider_text = provider_content if isinstance(provider_content, str) else provider_content[0]["text"]
        provider_payload = json.loads(provider_text)
        assert provider_payload["user_message"] == [
            {"type": "text", "text": "语音转写内容", "input_source": "voice"},
            {"type": "text", "text": "普通文本"},
        ]
        assert provider_payload["attachment_paths"] == ["virtual.png", "virtual.pdf"]

    history_result = await db_session.execute(select(Message).where(Message.id.in_(expected_message_ids)))
    history_by_id = {message.id: message for message in history_result.scalars().all()}
    history_rows = [history_by_id[message_id] for message_id in expected_message_ids]
    stored_contents = [message.content for message in history_rows]
    assert json.loads(history_rows[0].content) == [
        {"type": "text", "text": "语音转写内容", "input_source": "voice"},
    ]
    assert history_rows[1].content == "普通文本"
    history_messages = parse_db_messages_to_internal(history_rows)
    assert [message.id for message in history_messages] == expected_message_ids
    materialized_history = materialize_user_environment_prompts(history_messages)
    history_payloads = [json.loads(message.content) for message in materialized_history]
    assert history_payloads[0]["user_message"] == [
        {"type": "text", "text": "语音转写内容", "input_source": "voice"},
    ]
    assert history_payloads[1]["user_message"] == "普通文本"
    assert [message.content for message in history_rows] == stored_contents


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "legacy"])
async def test_running_foreground_append_preserves_voice_source(db_session: AsyncSession, mode: str):
    manager = SessionReplyQueueManager()
    initial_message, first_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="初始普通输入",
        attachments=None,
        source="http",
    )
    first_work.status = SessionReplyWorkStatus.RUNNING
    first_work.locked_by = "worker-1"
    db_session.add(first_work)
    await db_session.commit()
    _, _, frozen_message_ids = await manager.freeze_foreground_input(
        db_session,
        work=first_work,
        worker_id="worker-1",
    )
    assert frozen_message_ids == [initial_message.id]

    voice_request = ChatCompletionRequest(
        message=[{"type": "text", "text": "追加语音转写", "input_source": "voice"}],
        attachments=["virtual.png", "shared.pdf"],
    )
    voice_message, _ = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message=voice_request.message,
        attachments=voice_request.attachments,
        source="http",
    )
    text_message, _ = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="补充文字",
        attachments=["shared.pdf", "virtual.png"],
        source="http",
    )
    expected_message_ids = (voice_message.id, text_message.id)
    voice_row = await db_session.get(Message, voice_message.id)
    text_row = await db_session.get(Message, text_message.id)
    original_contents = (voice_row.content, text_row.content)
    assert json.loads(voice_row.content) == [
        {"type": "text", "text": "追加语音转写", "input_source": "voice"},
    ]
    assert text_row.content == "补充文字"

    if mode == "queue":
        batch = await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first_work.id,
            worker_id="worker-1",
        )
    else:
        batch = await fetch_and_merge_new_user_messages(db_session, "session-1", "user-1")

    assert batch is not None
    assert batch.source_message_ids == expected_message_ids
    assert len(batch.messages) == 1
    appended_message = batch.messages[0]
    assert appended_message.attachments == ["virtual.png", "shared.pdf"]

    for transformer in (OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer):
        materialized_message = materialize_user_environment_prompts([appended_message])[0]
        provider_message = transformer.to_provider([materialized_message])[0]
        provider_content = provider_message["content"]
        provider_text = provider_content if isinstance(provider_content, str) else provider_content[0]["text"]
        provider_payload = json.loads(provider_text)
        assert provider_payload["user_message"] == [
            {"type": "text", "text": "追加语音转写", "input_source": "voice"},
            {"type": "text", "text": "补充文字"},
        ]
        assert provider_payload["attachment_paths"] == ["virtual.png", "shared.pdf"]

    persisted_rows = list((await db_session.execute(select(Message).where(Message.id.in_(expected_message_ids)))).scalars().all())
    persisted_by_id = {message.id: message for message in persisted_rows}
    persisted_messages = [persisted_by_id[message_id] for message_id in expected_message_ids]
    for message in persisted_messages:
        await db_session.refresh(message)
    assert [message.content for message in persisted_messages] == list(original_contents)
    assert all(message.is_processed is True for message in persisted_messages)
    initial_row = await db_session.get(Message, initial_message.id)
    await db_session.refresh(initial_row)
    assert initial_row.content == "初始普通输入"
    assert initial_row.is_processed is True
    assert initial_message.id not in batch.source_message_ids

    await db_session.refresh(first_work)
    if mode == "queue":
        assert first_work.input_message_ids == [initial_message.id, *expected_message_ids]
    else:
        assert first_work.input_message_ids == [initial_message.id]


@pytest.mark.asyncio
async def test_weixin_voice_and_attachments_survive_queue_and_multimodal_assembly(db_session: AsyncSession, tmp_path):
    image_path = tmp_path / "inbound.png"
    Image.new("RGB", (2, 2), color="red").save(image_path)
    file_path = tmp_path / "notes.pdf"

    async def resolve_inbound_image(_item):
        return image_path

    async def resolve_inbound_file(_item):
        return file_path

    item_list = [
        {"type": 3, "voice_item": {"text": "语音正文"}},
        {"type": 2, "image_item": {}},
        {"type": 4, "file_item": {}},
    ]
    extracted_content, attachment_paths = await extract_text_and_attachments(
        SimpleNamespace(
            resolve_inbound_image=resolve_inbound_image,
            resolve_inbound_file=resolve_inbound_file,
        ),
        item_list,
    )

    assert isinstance(extracted_content, list)
    assert [part.model_dump(mode="json") for part in extracted_content] == [
        {"type": "text", "text": "语音正文", "input_source": "voice"},
        {"type": "text", "text": f"[文件:{file_path.name}]"},
    ]
    assert attachment_paths == [str(image_path), str(file_path)]

    manager = SessionReplyQueueManager()
    initial_message, work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message=extracted_content,
        attachments=attachment_paths,
        source="weixin-openclaw",
    )
    assert initial_message.assembled_attachment_part_count == 0
    stored_row = await db_session.get(Message, initial_message.id)
    assert stored_row is not None
    original_content = stored_row.content
    original_attachments = list(stored_row.attachments or [])

    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = "worker-1"
    db_session.add(work)
    await db_session.commit()
    frozen_content, frozen_attachments, frozen_message_ids = await manager.freeze_foreground_input(
        db_session,
        work=work,
        worker_id="worker-1",
    )

    assert frozen_message_ids == [initial_message.id]
    assert frozen_attachments == attachment_paths
    assert isinstance(frozen_content, list)
    assert [part.model_dump(mode="json") for part in frozen_content] == [
        {"type": "text", "text": "语音正文", "input_source": "voice"},
        {"type": "text", "text": f"[文件:{file_path.name}]"},
    ]

    assembled = InternalMessage.from_user_input(
        content=frozen_content,
        attachments=frozen_attachments,
    )
    disabled = MessageAssembler.assemble(
        assembled,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
    disabled_dump = disabled.model_dump(mode="json")
    assert disabled.attachments == attachment_paths
    assert sum(isinstance(part, VoiceTextPart) and part.text == "语音正文" and part.input_source == "voice" for part in disabled.content) == 1
    assert sum(isinstance(part, TextPart) and part.text == f"[文件:{file_path.name}]" for part in disabled.content) == 1
    assert not any(isinstance(part, ImagePart) for part in disabled.content)
    assert sum(isinstance(part, FilePart) for part in disabled.content) == 1
    assert any(isinstance(part, TextPart) and f"[未开启图像理解无法解析图片: {image_path}]" in part.text for part in disabled.content)
    for _ in range(2):
        assert (
            MessageAssembler.assemble(
                disabled,
                image_understanding=False,
                audio_understanding=False,
                video_understanding=False,
            ).model_dump(mode="json")
            == disabled_dump
        )

    restored = InternalMessage.model_validate(disabled_dump)
    enabled = MessageAssembler.assemble(
        restored,
        image_understanding=True,
        audio_understanding=False,
        video_understanding=False,
    )
    enabled_dump = enabled.model_dump(mode="json")
    assert enabled.attachments == attachment_paths
    assert sum(isinstance(part, VoiceTextPart) and part.text == "语音正文" and part.input_source == "voice" for part in enabled.content) == 1
    assert sum(isinstance(part, TextPart) and part.text == f"[文件:{file_path.name}]" for part in enabled.content) == 1
    assert sum(isinstance(part, ImagePart) for part in enabled.content) == 1
    assert sum(isinstance(part, FilePart) for part in enabled.content) == 1
    assert (
        MessageAssembler.assemble(
            enabled,
            image_understanding=True,
            audio_understanding=False,
            video_understanding=False,
        ).model_dump(mode="json")
        == enabled_dump
    )
    restored_enabled = InternalMessage.model_validate(enabled_dump)
    assert (
        MessageAssembler.assemble(
            restored_enabled,
            image_understanding=True,
            audio_understanding=False,
            video_understanding=False,
        ).model_dump(mode="json")
        == enabled_dump
    )

    materialized = materialize_user_environment_prompts([enabled])[0]
    for transformer, image_provider_type in (
        (OpenAIChatCompletionsTransformer, "image_url"),
        (OpenAIResponsesTransformer, "input_image"),
    ):
        provider_message = transformer.to_provider([materialized])[0]
        provider_content = provider_message["content"]
        assert isinstance(provider_content, list)
        provider_payload = json.loads(provider_content[0]["text"])
        assert provider_payload["user_message"][0] == {
            "type": "text",
            "text": "语音正文",
            "input_source": "voice",
        }
        assert provider_payload["user_message"][1] == {"type": "text", "text": f"[文件:{file_path.name}]"}
        assert provider_payload["attachment_paths"] == attachment_paths
        assert sum(part.get("type") == image_provider_type for part in provider_content) == 1

    await db_session.refresh(stored_row)
    assert stored_row.content == original_content
    assert stored_row.attachments == original_attachments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "literal_text",
    [
        json.dumps([{"type": "text", "text": "普通文本"}], ensure_ascii=False),
        json.dumps([{"type": "text", "text": "原始语音", "input_source": "voice"}], ensure_ascii=False),
        json.dumps([{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}], ensure_ascii=False),
        json.dumps([{"type": "audio", "format": "wav", "data": "YQ=="}], ensure_ascii=False),
        json.dumps([{"type": "file", "path": "virtual.txt"}], ensure_ascii=False),
    ],
)
@pytest.mark.parametrize("mode", ["freeze", "queue", "legacy"])
async def test_plain_json_text_remains_text_across_freeze_history_and_provider(
    db_session: AsyncSession,
    literal_text: str,
    mode: str,
):
    manager = SessionReplyQueueManager()
    voice_content = [VoiceTextPart(text="真实语音", input_source="voice")]
    expected_parts = [
        voice_content[0].model_dump(mode="json"),
        TextPart(text=literal_text).model_dump(mode="json"),
    ]

    async def enqueue_message(message):
        return await manager._enqueue_foreground_message(
            db_session,
            uid="user-1",
            session_id="session-1",
            profile=SimpleNamespace(id=1),
            message=message,
            attachments=None,
            source="http",
        )

    first_message, first_work = await enqueue_message(voice_content if mode == "freeze" else "初始普通输入")
    first_work.status = SessionReplyWorkStatus.RUNNING
    first_work.locked_by = "worker-1"
    db_session.add(first_work)
    await db_session.commit()

    if mode == "freeze":
        voice_message = first_message
        literal_message, _ = await enqueue_message(literal_text)
        frozen_content, _, source_message_ids = await manager.freeze_foreground_input(
            db_session,
            work=first_work,
            worker_id="worker-1",
        )
        output_content = frozen_content
        assert source_message_ids == [voice_message.id, literal_message.id]
    else:
        _, _, initial_message_ids = await manager.freeze_foreground_input(
            db_session,
            work=first_work,
            worker_id="worker-1",
        )
        assert initial_message_ids == [first_message.id]
        voice_message, _ = await enqueue_message(voice_content)
        literal_message, _ = await enqueue_message(literal_text)
        if mode == "queue":
            batch = await manager.absorb_contiguous_foreground_messages(
                db_session,
                work_id=first_work.id,
                worker_id="worker-1",
            )
        else:
            batch = await fetch_and_merge_new_user_messages(db_session, "session-1", "user-1")
        assert batch is not None
        assert batch.source_message_ids == (voice_message.id, literal_message.id)
        assert len(batch.messages) == 1
        assert batch.messages[0].assembled_attachment_part_count == 0
        output_content = batch.messages[0].content

    assert isinstance(output_content, list)
    assert [part.model_dump(mode="json") for part in output_content] == expected_parts

    voice_row = await db_session.get(Message, voice_message.id)
    literal_row = await db_session.get(Message, literal_message.id)
    assert voice_row is not None
    assert literal_row is not None
    original_contents = (voice_row.content, literal_row.content)
    await db_session.refresh(voice_row)
    await db_session.refresh(literal_row)
    assert voice_row.content_format == MessageContentFormat.PARTS
    assert literal_row.content_format == MessageContentFormat.TEXT
    assert voice_row.content == original_contents[0]
    assert literal_row.content == literal_text == original_contents[1]
    assert json.loads(voice_row.content) == expected_parts[:1]

    voice_response = MessageResponse.model_validate(voice_row)
    literal_response = MessageResponse.model_validate(literal_row)
    assert voice_response.content == expected_parts[:1]
    assert literal_response.content == literal_text

    parsed_history = parse_db_messages_to_internal([voice_row, literal_row])
    assert [message.id for message in parsed_history] == [voice_message.id, literal_message.id]
    assert isinstance(parsed_history[0].content, list)
    assert [part.model_dump(mode="json") for part in parsed_history[0].content] == expected_parts[:1]
    assert parsed_history[1].content == literal_text

    provider_input = InternalMessage(role=MessageRole.USER, content=output_content)
    media_provider_types = {"image_url", "input_image", "input_audio", "audio", "file", "input_file"}
    for transformer in (OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer):
        materialized = materialize_user_environment_prompts([provider_input])[0]
        provider_message = transformer.to_provider([materialized])[0]
        provider_content = provider_message["content"]
        provider_parts = provider_content if isinstance(provider_content, list) else []
        provider_text = provider_content if isinstance(provider_content, str) else next(part["text"] for part in provider_parts if isinstance(part, dict) and isinstance(part.get("text"), str))
        provider_payload = json.loads(provider_text)
        assert provider_payload["user_message"] == expected_parts
        assert not any(isinstance(part, dict) and part.get("type") in media_provider_types for part in provider_parts)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_parts", [True, False])
async def test_idempotent_submit_distinguishes_content_format(database_factory, first_is_parts: bool):
    parts = [VoiceTextPart(text="转写", input_source="voice")]
    literal = json.dumps([part.model_dump(mode="json") for part in parts], ensure_ascii=False)
    first_input = parts if first_is_parts else literal
    other_input = literal if first_is_parts else parts
    expected_content_format = MessageContentFormat.PARTS if first_is_parts else MessageContentFormat.TEXT

    async with database_factory() as session_factory:
        async with session_factory() as db_session:
            profile = Profile(id=1, uid="user-1", name="format-identity", configs={})
            db_session.add(profile)
            await db_session.commit()

            manager = SessionReplyQueueManager()
            first_message, first_work, _first_status, _first_events = await manager.submit_user_message(
                db_session,
                uid="user-1",
                session_id="session-1",
                profile=profile,
                message=first_input,
                attachments=None,
                source="http",
                request_id="format-identity",
                idempotent_http_request=True,
            )
            repeated_message, repeated_work, _repeated_status, _repeated_events = await manager.submit_user_message(
                db_session,
                uid="user-1",
                session_id="session-1",
                profile=profile,
                message=first_input,
                attachments=None,
                source="http",
                request_id="format-identity",
                idempotent_http_request=True,
            )

            expected_content = parts if first_is_parts else literal
            assert first_message.id == repeated_message.id
            assert first_work.id == repeated_work.id
            assert first_message.content == expected_content
            assert repeated_message.content == expected_content
            assert first_message.assembled_attachment_part_count == 0
            assert repeated_message.assembled_attachment_part_count == 0
            assert type(repeated_message.content) is type(first_message.content)
            assert isinstance(first_message.content, list) is first_is_parts
            assert isinstance(repeated_message.content, list) is first_is_parts

            with pytest.raises(ParameterException) as exc_info:
                await manager.submit_user_message(
                    db_session,
                    uid="user-1",
                    session_id="session-1",
                    profile=profile,
                    message=other_input,
                    attachments=None,
                    source="http",
                    request_id="format-identity",
                    idempotent_http_request=True,
                )
            assert exc_info.value.message == ERR_CHAT_REQUEST_ID_CONFLICT

            rows = list(
                (
                    await db_session.execute(
                        select(Message).where(
                            Message.session_id == "session-1",
                            Message.uid == "user-1",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            persisted = rows[0]
            assert persisted.id == first_message.id
            assert persisted.content_format == expected_content_format
            assert persisted.content == literal
            assert persisted.content.encode("utf-8") == literal.encode("utf-8")
