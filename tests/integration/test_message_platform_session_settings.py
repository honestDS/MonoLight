from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.crypto
import app.core.dispatcher as dispatcher_module
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.outbound import build_weixin_openclaw_concise_output_system_prompt
from app.core.dispatcher import ChatDispatcher
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole, MessageType
from app.models.message_platform import MessagePlatform, MessagePlatformStatus, MessagePlatformType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User

UID = "owner"
EXTERNAL_USER = "external-user"
SESSION_ID = "weixin-openclaw:external-user"
MODEL_ID = "message-platform-session-model"
FINAL_TEXT = "最终回复"
FINAL_REASONING = "推理不丢失"


def _profile_configs(channel_id: int) -> dict[str, Any]:
    return {
        "channel": {
            "chat_channel": {
                "chat_timeout": 60,
                "rules": [
                    {
                        "channel_id": channel_id,
                        "model_id": MODEL_ID,
                        "priority": 1,
                        "weight": 1,
                        "is_enabled": True,
                    }
                ],
            }
        },
        "security": {"audit_threshold": 0},
        "tool": {"enabled_tools": [], "max_parallel_tools": 1},
        "other": {"context_summary_threshold_percent": 90},
        "memory": {"enabled": False, "precheck_enabled": False},
    }


def _session_settings(
    *,
    enable_markdown: bool,
    goal_mode: bool = False,
    max_turns: int = 7,
    show_tool_calls: bool = True,
    show_reasoning: bool = False,
) -> dict[str, Any]:
    return {
        "enable_markdown": enable_markdown,
        "goal_mode": goal_mode,
        "max_turns": max_turns,
        "show_tool_calls": show_tool_calls,
        "show_reasoning": show_reasoning,
    }


async def _seed_platform_data(
    factory: async_sessionmaker[AsyncSession],
    *,
    stream_llm_request: bool,
    session_settings: dict[str, Any],
    include_legacy_platform: bool = False,
) -> dict[str, int]:
    async with factory() as db:
        db.add(User(uid=UID, username="platform-session-owner"))
        channel = ModelChannel(
            name="message-platform-session-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid/v1",
            model_ids=[
                {
                    "model_id": MODEL_ID,
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 32,
                    "max_tokens": 512,
                    "temperature": 0,
                    "top_p": 1,
                    "is_enabled": True,
                }
            ],
        )
        db.add(channel)
        await db.flush()
        assert channel.id is not None

        primary_profile = Profile(
            uid=UID,
            name="message-platform-session-default",
            configs=_profile_configs(channel.id),
            is_default=True,
        )
        override_profile = Profile(
            uid=UID,
            name="message-platform-session-override",
            configs=_profile_configs(channel.id),
            is_default=False,
        )
        db.add_all([primary_profile, override_profile])
        await db.flush()
        assert primary_profile.id is not None and override_profile.id is not None

        platform = MessagePlatform(
            name="message-platform-session",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            is_enabled=True,
            status=MessagePlatformStatus.CONNECTED,
            uid=UID,
            profile_id=primary_profile.id,
            config={
                "token": "weixin-session-token",
                "stream_llm_request": stream_llm_request,
                "session_settings": session_settings,
            },
            state={"context_tokens": {EXTERNAL_USER: "context-token"}},
        )
        db.add(platform)

        legacy_platform = None
        if include_legacy_platform:
            legacy_platform = MessagePlatform(
                name="message-platform-session-legacy",
                platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                is_enabled=True,
                status=MessagePlatformStatus.CONNECTED,
                uid=UID,
                profile_id=primary_profile.id,
                config={
                    "token": "legacy-weixin-session-token",
                    "stream_llm_request": False,
                },
                state={"context_tokens": {"legacy-external-user": "legacy-context-token"}},
            )
            db.add(legacy_platform)

        await db.commit()
        assert platform.id is not None
        result = {
            "platform_id": platform.id,
            "primary_profile_id": primary_profile.id,
            "override_profile_id": override_profile.id,
        }
        if legacy_platform is not None:
            assert legacy_platform.id is not None
            result["legacy_platform_id"] = legacy_platform.id
        return result


def _patch_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")


def _patch_runtime_database(monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]) -> None:
    _patch_external_dependencies(monkeypatch)
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)


def _patch_llm(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[list[dict[str, Any]]]]:
    branches: list[str] = []
    request_messages: list[list[dict[str, Any]]] = []
    response = InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=FINAL_TEXT,
            reasoning_content=FINAL_REASONING,
        ),
        model=MODEL_ID,
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        finish_reason="stop",
    )

    def capture_request(kwargs: dict[str, Any]) -> None:
        request_messages.append([message.model_dump(mode="json") for message in kwargs["messages"]])

    async def generate(**kwargs: Any) -> InternalResponse:
        branches.append("generate")
        capture_request(kwargs)
        return response

    async def generate_with_stream_callback(**kwargs: Any) -> InternalResponse:
        branches.append("generate_with_stream_callback")
        capture_request(kwargs)
        await kwargs["on_reasoning"](FINAL_REASONING)
        await kwargs["on_content"](FINAL_TEXT)
        return response

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    return branches, request_messages


async def _load_initial_message(
    factory: async_sessionmaker[AsyncSession],
    message_id: int,
) -> InternalMessage:
    async with factory() as db:
        message = await db.get(Message, message_id)
        assert message is not None
        assert message.id is not None
        return InternalMessage(
            id=message.id,
            role=message.role,
            content=message.content,
            reasoning_content=message.reasoning_content,
            provider_metadata=message.provider_metadata,
            environment_prompt=message.environment_prompt,
            guidance_prompt=message.guidance_prompt,
            attachments=message.attachments,
            created_at=message.created_at.timestamp(),
        )


async def _dispatch_queued_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    work_id: int,
    message_id: int,
    profile_id: int,
    stream_dispatch: bool,
) -> dict[str, Any] | list[dict[str, Any]]:
    initial_message = await _load_initial_message(factory, message_id)
    async with factory() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        assert work is not None
        execution_state = work.execution_state or {}
        dispatch_kwargs = {
            "db": db,
            "message": "hello",
            "uid": UID,
            "session_id": SESSION_ID,
            "session_source": "weixin-openclaw",
            "persisted_initial_message": initial_message,
            "frozen_user_message_ids": [message_id],
            "persisted_profile_id": profile_id,
            "show_tool_calls": bool(execution_state.get("show_tool_calls", True)),
            "expose_tool_call_content": bool(execution_state.get("expose_tool_call_content", True)),
            "additional_system_prompt": execution_state.get("additional_system_prompt"),
        }
        if stream_dispatch:
            return [
                event
                async for event in ChatDispatcher.dispatch_stream(
                    **dispatch_kwargs,
                    request_id="message-platform-session-request",
                )
            ]
        return await ChatDispatcher.dispatch(**dispatch_kwargs)


def _extract_response_settings(request_messages: list[list[dict[str, Any]]]) -> tuple[dict[str, Any], str]:
    assert len(request_messages) == 1
    user_messages = [message for message in request_messages[0] if message.get("role") == MessageRole.USER.value and isinstance(message.get("content"), str)]
    assert user_messages
    payload = json.loads(user_messages[-1]["content"])
    assert isinstance(payload, dict)
    response_settings = payload.get("response_settings")
    platform_constraints = payload.get("platform_constraints")
    assert isinstance(response_settings, dict)
    assert isinstance(platform_constraints, str)
    return response_settings, platform_constraints


@pytest.mark.asyncio
@pytest.mark.parametrize("return_stream", [False, True], ids=["return-non-stream", "return-stream"])
@pytest.mark.parametrize("upstream_stream", [False, True], ids=["upstream-non-stream", "upstream-stream"])
async def test_message_platform_session_settings_flow_through_queue_and_dispatcher(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    return_stream: bool,
    upstream_stream: bool,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=upstream_stream,
            session_settings=_session_settings(enable_markdown=upstream_stream),
        )
        _patch_runtime_database(monkeypatch, factory)
        adapter = object.__new__(WeixinOpenClawAdapter)

        async with factory() as db:
            result = await adapter.chat(
                db,
                "hello",
                UID,
                SESSION_ID,
                message_platform_id=data["platform_id"],
                stream_requested=return_stream,
            )
            assert result.text == ""
            assert result.files == []

        async with factory() as db:
            session = await db.get(ChatSession, SESSION_ID)
            assert session is not None
            assert {
                "enable_markdown": session.enable_markdown,
                "goal_mode": session.goal_mode,
                "max_turns": session.max_turns,
                "show_tool_calls": session.show_tool_calls,
                "show_reasoning": session.show_reasoning,
            } == _session_settings(enable_markdown=upstream_stream)

            messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all()
            assert len(messages) == 1
            initial_message = messages[0]
            assert initial_message.id is not None
            assert initial_message.role == MessageRole.USER
            assert initial_message.type == MessageType.TEXT
            assert initial_message.content == "hello"

            works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == SESSION_ID))).scalars().all()
            assert len(works) == 1
            work = works[0]
            assert work.id is not None
            assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert work.execution_state["stream_requested"] is return_stream
            work_id = work.id

        branches, request_messages = _patch_llm(monkeypatch)
        dispatch_result = await _dispatch_queued_message(
            factory,
            work_id=work_id,
            message_id=initial_message.id,
            profile_id=data["primary_profile_id"],
            stream_dispatch=return_stream,
        )

        assert branches == ["generate_with_stream_callback" if upstream_stream else "generate"]
        response_settings, platform_constraints = _extract_response_settings(request_messages)
        assert response_settings["markdown"] is upstream_stream
        assert platform_constraints == build_weixin_openclaw_concise_output_system_prompt()
        assert "markdown" not in platform_constraints.lower()
        assert "utf-8" in platform_constraints.lower()

        if return_stream:
            assert isinstance(dispatch_result, list)
            done_events = [event for event in dispatch_result if event.get("type") == "done"]
            turn_end_events = [event for event in dispatch_result if event.get("type") == "turn_end"]
            assert len(done_events) == 1
            assert len(turn_end_events) == 1
            response = done_events[0]["response"]
            assert turn_end_events[0]["content"] == FINAL_TEXT
            assert turn_end_events[0]["reasoning_content"] == FINAL_REASONING
        else:
            assert isinstance(dispatch_result, dict)
            response = dispatch_result

        response_message = response["choices"][0]["message"]
        assert response_message["content"] == FINAL_TEXT
        assert response_message["reasoning_content"] == FINAL_REASONING

        async with factory() as db:
            persisted_messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all()
            user_messages = [message for message in persisted_messages if message.role == MessageRole.USER]
            assistant_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT]
            assert len(user_messages) == 1
            assert user_messages[0].content == "hello"
            assert len(assistant_messages) == 1
            assert assistant_messages[0].content == FINAL_TEXT
            assert assistant_messages[0].reasoning_content == FINAL_REASONING


@pytest.mark.asyncio
async def test_message_platform_session_settings_update_existing_and_legacy_sessions(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(
                enable_markdown=True,
                goal_mode=True,
                max_turns=5,
                show_tool_calls=False,
                show_reasoning=True,
            ),
            include_legacy_platform=True,
        )
        _patch_runtime_database(monkeypatch, factory)
        adapter = object.__new__(WeixinOpenClawAdapter)

        async with factory() as db:
            first = await adapter.chat(
                db,
                "first",
                UID,
                SESSION_ID,
                message_platform_id=data["platform_id"],
                stream_requested=False,
            )
            assert first.text == ""

        async with factory() as db:
            session = await db.get(ChatSession, SESSION_ID)
            assert session is not None
            session.profile_override_id = data["override_profile_id"]
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            platform.config = {
                **(platform.config or {}),
                "session_settings": {"max_turns": 11},
            }
            await db.commit()

        async with factory() as db:
            second = await adapter.chat(
                db,
                "second",
                UID,
                SESSION_ID,
                message_platform_id=data["platform_id"],
                stream_requested=True,
            )
            assert second.text == ""

        async with factory() as db:
            session = await db.get(ChatSession, SESSION_ID)
            assert session is not None
            assert session.profile_id == data["override_profile_id"]
            assert session.profile_override_id == data["override_profile_id"]
            assert {
                "enable_markdown": session.enable_markdown,
                "goal_mode": session.goal_mode,
                "max_turns": session.max_turns,
                "show_tool_calls": session.show_tool_calls,
                "show_reasoning": session.show_reasoning,
            } == _session_settings(
                enable_markdown=True,
                goal_mode=True,
                max_turns=11,
                show_tool_calls=False,
                show_reasoning=True,
            )

            legacy_session_id = "weixin-openclaw:legacy-external-user"
            db.add(
                ChatSession(
                    session_id=legacy_session_id,
                    uid=UID,
                    profile_id=data["primary_profile_id"],
                    profile_override_id=data["override_profile_id"],
                    source="weixin-openclaw",
                    enable_markdown=False,
                    goal_mode=True,
                    max_turns=3,
                    show_tool_calls=False,
                    show_reasoning=True,
                )
            )
            await db.commit()

        async with factory() as db:
            legacy = await adapter.chat(
                db,
                "legacy",
                UID,
                "weixin-openclaw:legacy-external-user",
                message_platform_id=data["legacy_platform_id"],
                stream_requested=True,
            )
            assert legacy.text == ""

        async with factory() as db:
            legacy_session = await db.get(ChatSession, "weixin-openclaw:legacy-external-user")
            assert legacy_session is not None
            assert legacy_session.profile_id == data["override_profile_id"]
            assert legacy_session.profile_override_id == data["override_profile_id"]
            assert {
                "enable_markdown": legacy_session.enable_markdown,
                "goal_mode": legacy_session.goal_mode,
                "max_turns": legacy_session.max_turns,
                "show_tool_calls": legacy_session.show_tool_calls,
                "show_reasoning": legacy_session.show_reasoning,
            } == {
                "enable_markdown": False,
                "goal_mode": True,
                "max_turns": 3,
                "show_tool_calls": False,
                "show_reasoning": True,
            }
