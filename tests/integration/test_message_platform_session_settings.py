from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.adapters.weixin_openclaw.adapter as weixin_openclaw_adapter_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.crypto
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.message_platforms.manager as message_platform_manager_module
import app.core.message_platforms.notifier as message_platform_notifier_module
import app.core.message_platforms.weixin_openclaw as weixin_openclaw_platform_module
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.outbound import build_weixin_openclaw_concise_output_system_prompt
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.api.v1 import chat as chat_api
from app.api.v1 import message_platforms as message_platforms_api
from app.core.constants import (
    ERR_MESSAGE_PLATFORM_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
)
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole, MessageType
from app.models.message_platform import MessagePlatform, MessagePlatformStatus, MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from app.providers.database import get_db

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


async def _seed_second_platform(
    factory: async_sessionmaker[AsyncSession],
    *,
    profile_id: int,
    stream_llm_request: bool,
    context_token: str | None,
) -> int:
    async with factory() as db:
        context_tokens = {EXTERNAL_USER: context_token} if context_token is not None else {}
        platform = MessagePlatform(
            name="message-platform-session-second",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            is_enabled=True,
            status=MessagePlatformStatus.CONNECTED,
            uid=UID,
            profile_id=profile_id,
            config={
                "token": "weixin-session-second-token",
                "stream_llm_request": stream_llm_request,
            },
            state={"context_tokens": context_tokens},
        )
        db.add(platform)
        await db.commit()
        assert platform.id is not None
        return platform.id


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
            "session_id": work.session_id,
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
async def test_message_platform_defaults_do_not_overwrite_existing_and_legacy_sessions(
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
            assert session.stream_llm_request is False
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
            assert session.stream_llm_request is False
            assert {
                "enable_markdown": session.enable_markdown,
                "goal_mode": session.goal_mode,
                "max_turns": session.max_turns,
                "show_tool_calls": session.show_tool_calls,
                "show_reasoning": session.show_reasoning,
            } == _session_settings(
                enable_markdown=True,
                goal_mode=True,
                max_turns=5,
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
            assert legacy_session.stream_llm_request is False
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


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_dispatch", [False, True], ids=["dispatch-non-stream", "dispatch-stream"])
async def test_weixin_openclaw_handle_message_isolates_same_legacy_session_id_per_platform(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    stream_dispatch: bool,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        second_platform_id = await _seed_second_platform(
            factory,
            profile_id=data["override_profile_id"],
            stream_llm_request=True,
            context_token="second-context-token",
        )
        _patch_runtime_database(monkeypatch, factory)
        _patch_llm(monkeypatch)

        async def no_op_title(**_: Any) -> None:
            return None

        monkeypatch.setattr(weixin_openclaw_adapter_module, "generate_session_title_for_selected_profile", no_op_title)
        replies: list[str] = []

        async def capture_reply(user_id: str, text: str, *, context_token: str = "") -> bool:
            replies.append(text)
            return True

        adapter = object.__new__(WeixinOpenClawAdapter)
        monkeypatch.setattr(adapter, "reply_text", capture_reply)

        async with factory() as db:
            first_message = WeixinOpenClawMessage(
                user_id=EXTERNAL_USER,
                text="from-platform-one",
                session_id=SESSION_ID,
                context_token="context-token",
            )
            first = await adapter.handle_message(
                db,
                first_message,
                uid=UID,
                message_platform_id=data["platform_id"],
            )
            second_message = WeixinOpenClawMessage(
                user_id=EXTERNAL_USER,
                text="from-platform-two",
                session_id=SESSION_ID,
                context_token="second-context-token",
            )
            second = await adapter.handle_message(
                db,
                second_message,
                uid=UID,
                message_platform_id=second_platform_id,
            )
            assert first is True
            assert second is True

        async with factory() as db:
            sessions = (await db.execute(select(ChatSession).where(ChatSession.uid == UID, ChatSession.source == "weixin-openclaw").order_by(ChatSession.message_platform_id))).scalars().all()
            assert len(sessions) == 2
            assert len({session.session_id for session in sessions}) == 2
            sessions_by_platform = {session.message_platform_id: session for session in sessions}
            assert set(sessions_by_platform) == {data["platform_id"], second_platform_id}

            first_session = sessions_by_platform[data["platform_id"]]
            second_session = sessions_by_platform[second_platform_id]
            assert first_session.message_platform_user_id == EXTERNAL_USER
            assert second_session.message_platform_user_id == EXTERNAL_USER
            assert first_session.session_id == first_message.session_id
            assert second_session.session_id == second_message.session_id
            assert first_session.profile_id == data["primary_profile_id"]
            assert second_session.profile_id == data["override_profile_id"]

            first_messages = (await db.execute(select(Message).where(Message.session_id == first_session.session_id, Message.role == MessageRole.USER).order_by(Message.id))).scalars().all()
            second_messages = (await db.execute(select(Message).where(Message.session_id == second_session.session_id, Message.role == MessageRole.USER).order_by(Message.id))).scalars().all()
            assert len(first_messages) == 1
            assert len(second_messages) == 1
            assert first_messages[0].content == "from-platform-one"
            assert second_messages[0].content == "from-platform-two"

            first_works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == first_session.session_id))).scalars().all()
            second_works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == second_session.session_id))).scalars().all()
            assert len(first_works) == 1
            assert len(second_works) == 1
            assert first_works[0].profile_id == data["primary_profile_id"]
            assert second_works[0].profile_id == data["override_profile_id"]

            first_work = first_works[0]
            second_work = second_works[0]
            first_message_id = first_messages[0].id
            second_message_id = second_messages[0].id
            first_work_id = first_work.id
            second_work_id = second_work.id
            assert first_message_id is not None
            assert second_message_id is not None
            assert first_work_id is not None
            assert second_work_id is not None

        branches, _ = _patch_llm(monkeypatch)
        first_dispatch_result = await _dispatch_queued_message(
            factory,
            work_id=first_work_id,
            message_id=first_message_id,
            profile_id=data["primary_profile_id"],
            stream_dispatch=stream_dispatch,
        )
        second_dispatch_result = await _dispatch_queued_message(
            factory,
            work_id=second_work_id,
            message_id=second_message_id,
            profile_id=data["override_profile_id"],
            stream_dispatch=stream_dispatch,
        )

        assert branches == ["generate", "generate_with_stream_callback"]
        for dispatch_result in (first_dispatch_result, second_dispatch_result):
            if stream_dispatch:
                assert isinstance(dispatch_result, list)
                done_events = [event for event in dispatch_result if event.get("type") == "done"]
                assert len(done_events) == 1
                response = done_events[0]["response"]
            else:
                assert isinstance(dispatch_result, dict)
                response = dispatch_result
            assert response["choices"][0]["message"]["content"] == FINAL_TEXT

        assert replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "both_have_context_token",
    [True, False],
    ids=["both-platforms-have-context-token", "neither-platform-has-context-token"],
)
async def test_get_platform_for_legacy_session_returns_none_when_platform_match_is_ambiguous(
    database_factory,
    both_have_context_token: bool,
) -> None:
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        second_platform_id = await _seed_second_platform(
            factory,
            profile_id=data["primary_profile_id"],
            stream_llm_request=True,
            context_token="second-context-token" if both_have_context_token else None,
        )

        async with factory() as db:
            first_platform = await db.get(MessagePlatform, data["platform_id"])
            second_platform = await db.get(MessagePlatform, second_platform_id)
            assert first_platform is not None
            assert second_platform is not None
            context_tokens = {EXTERNAL_USER: "context-token"} if both_have_context_token else {}
            first_platform.state = {"context_tokens": context_tokens}
            second_platform.state = {"context_tokens": context_tokens if both_have_context_token else {}}
            db.add(
                ChatSession(
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=data["primary_profile_id"],
                    source="weixin-openclaw",
                )
            )
            await db.commit()

        async with factory() as db:
            platform = await message_platform_crud.get_platform_for_session(
                db,
                uid=UID,
                session_id=SESSION_ID,
                source="weixin-openclaw",
            )
            assert platform is None


@pytest.mark.asyncio
async def test_weixin_openclaw_same_platform_user_supports_opaque_multi_sessions_and_outbox_delivery(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        second_platform_id = await _seed_second_platform(
            factory,
            profile_id=data["override_profile_id"],
            stream_llm_request=True,
            context_token="second-context-token",
        )
        _patch_runtime_database(monkeypatch, factory)
        monkeypatch.setattr(message_platform_notifier_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(message_platform_manager_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(weixin_openclaw_platform_module, "AsyncSessionLocal", factory)

        async def no_op_title(**_: Any) -> None:
            return None

        monkeypatch.setattr(weixin_openclaw_adapter_module, "generate_session_title_for_selected_profile", no_op_title)
        replies: list[str] = []

        async def capture_reply(user_id: str, text: str, *, context_token: str = "") -> bool:
            replies.append(text)
            return True

        adapter = object.__new__(WeixinOpenClawAdapter)
        monkeypatch.setattr(adapter, "reply_text", capture_reply)
        session_a_id = "opaque-conversation-a"
        session_b_id = "opaque-conversation-b"

        async with factory() as db:
            first = await adapter.chat(
                db,
                "first",
                UID,
                session_a_id,
                message_platform_id=second_platform_id,
                message_platform_user_id=EXTERNAL_USER,
            )
            assert first.text == ""
            session_a = await db.get(ChatSession, session_a_id)
            assert session_a is not None
            session_a.created_at = datetime(2026, 1, 1, tzinfo=UTC)
            await db.commit()

        async with factory() as db:
            second = await adapter.chat(
                db,
                "second",
                UID,
                session_b_id,
                message_platform_id=second_platform_id,
                message_platform_user_id=EXTERNAL_USER,
            )
            assert second.text == ""
            session_b = await db.get(ChatSession, session_b_id)
            assert session_b is not None
            session_b.created_at = datetime(2026, 1, 2, tzinfo=UTC)
            await db.commit()

        follow_up_message = WeixinOpenClawMessage(
            user_id=EXTERNAL_USER,
            text="follow-up",
            session_id=SESSION_ID,
            context_token="second-context-token",
        )
        async with factory() as db:
            handled = await adapter.handle_message(
                db,
                follow_up_message,
                uid=UID,
                message_platform_id=second_platform_id,
            )
            assert handled is True

        async with factory() as db:
            sessions = (
                (
                    await db.execute(
                        select(ChatSession)
                        .where(
                            ChatSession.uid == UID,
                            ChatSession.message_platform_id == second_platform_id,
                            ChatSession.message_platform_user_id == EXTERNAL_USER,
                        )
                        .order_by(ChatSession.created_at)
                    )
                )
                .scalars()
                .all()
            )
            assert len(sessions) == 2
            assert [session.session_id for session in sessions] == [session_a_id, session_b_id]
            assert all(session.message_platform_id == second_platform_id for session in sessions)
            assert all(session.message_platform_user_id == EXTERNAL_USER for session in sessions)

            first_session_messages = (await db.execute(select(Message).where(Message.session_id == session_a_id, Message.role == MessageRole.USER).order_by(Message.id))).scalars().all()
            second_session_messages = (await db.execute(select(Message).where(Message.session_id == session_b_id, Message.role == MessageRole.USER).order_by(Message.id))).scalars().all()
            assert [message.content for message in first_session_messages] == ["first"]
            assert [message.content for message in second_session_messages] == ["second", "follow-up"]
            assert replies == []

        requests: list[tuple[str, dict[str, Any]]] = []

        async def capture_request(
            self: WeixinOpenClawAdapter,
            method: str,
            endpoint: str,
            *,
            params: dict[str, Any] | None = None,
            payload: dict[str, Any] | None = None,
            token_required: bool = True,
            timeout_ms: int | None = None,
            headers: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            assert payload is not None
            requests.append((self.config.token, payload))
            return {}

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", capture_request)
        await message_platform_notifier_module.send_session_event(
            UID,
            session_a_id,
            {
                "event_id": "opaque-session-reply-a",
                "type": "proactive_reply",
                "content": "reply-a",
            },
        )
        await message_platform_notifier_module.send_session_event(
            UID,
            session_b_id,
            {
                "event_id": "opaque-session-reply-b",
                "type": "proactive_reply",
                "content": "reply-b",
            },
        )

        polling_manager = MessagePlatformPollingManager((WeixinOpenClawPlatformHandler(),))
        assert await polling_manager.process_outbox_batch() == 2

        async with factory() as db:
            outbox_items = (await db.execute(select(MessagePlatformOutbox).where(MessagePlatformOutbox.uid == UID).order_by(MessagePlatformOutbox.session_id))).scalars().all()
            assert len(outbox_items) == 2
            assert {item.session_id for item in outbox_items} == {session_a_id, session_b_id}
            assert {item.status for item in outbox_items} == {MessagePlatformOutboxStatus.SENT}

        assert len(requests) == 2
        requests_by_text = {payload["msg"]["item_list"][0]["text_item"]["text"]: (token, payload) for token, payload in requests}
        assert set(requests_by_text) == {"reply-a", "reply-b"}
        for text in ("reply-a", "reply-b"):
            token, payload = requests_by_text[text]
            assert token == "weixin-session-second-token"
            assert payload["msg"]["to_user_id"] == EXTERNAL_USER
            assert payload["msg"]["context_token"] == "second-context-token"
            assert [item["text_item"]["text"] for item in payload["msg"]["item_list"]] == [text]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["platform", "recipient"], ids=["platform", "recipient"])
async def test_message_platform_session_binding_rejects_rebind_and_rolls_back(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        second_platform_id = await _seed_second_platform(
            factory,
            profile_id=data["override_profile_id"],
            stream_llm_request=False,
            context_token="second-context-token",
        )
        _patch_runtime_database(monkeypatch, factory)
        adapter = object.__new__(WeixinOpenClawAdapter)
        session_id = "opaque-binding-session"

        async with factory() as db:
            first = await adapter.chat(
                db,
                "first",
                UID,
                session_id,
                message_platform_id=data["platform_id"],
                message_platform_user_id=EXTERNAL_USER,
            )
            assert first.text == ""

        async with factory() as db:
            session = await db.get(ChatSession, session_id)
            assert session is not None
            original_binding = (
                session.message_platform_id,
                session.message_platform_user_id,
                session.profile_id,
            )
            assert original_binding == (data["platform_id"], EXTERNAL_USER, data["primary_profile_id"])

        platform_id = second_platform_id if change == "platform" else data["platform_id"]
        platform_user_id = EXTERNAL_USER if change == "platform" else "different-user"
        async with factory() as db:
            forbidden = await adapter.chat(
                db,
                "forbidden",
                UID,
                session_id,
                message_platform_id=platform_id,
                message_platform_user_id=platform_user_id,
            )
            assert forbidden.text == t(ERR_SESSION_NO_PERMISSION)

        async with factory() as db:
            session = await db.get(ChatSession, session_id)
            assert session is not None
            assert (
                session.message_platform_id,
                session.message_platform_user_id,
                session.profile_id,
            ) == original_binding

            messages = (await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all()
            works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all()
            assert len(messages) == 1
            assert len(works) == 1
            assert [message.content for message in messages if message.role == MessageRole.USER] == ["first"]

        async with factory() as db:
            allowed = await adapter.chat(
                db,
                "allowed",
                UID,
                session_id,
                message_platform_id=data["platform_id"],
                message_platform_user_id=EXTERNAL_USER,
            )
            assert allowed.text == ""

        async with factory() as db:
            messages = (await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all()
            works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all()
            assert len(messages) == 2
            assert len(works) == 2
            assert [message.content for message in messages if message.role == MessageRole.USER] == ["first", "allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["ambiguous_legacy", "disabled", "deleted", "owner_changed"],
    ids=["ambiguous-legacy", "disabled", "deleted", "owner-changed"],
)
@pytest.mark.parametrize("stream_dispatch", [False, True], ids=["dispatch-non-stream", "dispatch-stream"])
async def test_message_platform_source_invalid_does_not_dispatch_or_fallback(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    stream_dispatch: bool,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        await _seed_second_platform(
            factory,
            profile_id=data["primary_profile_id"],
            stream_llm_request=True,
            context_token="second-context-token",
        )
        _patch_runtime_database(monkeypatch, factory)
        adapter = object.__new__(WeixinOpenClawAdapter)

        async with factory() as db:
            result = await adapter.chat(
                db,
                "first",
                UID,
                SESSION_ID,
                message_platform_id=data["platform_id"],
            )
            assert result.text == ""

        async with factory() as db:
            session = await db.get(ChatSession, SESSION_ID)
            assert session is not None
            message = (await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.role == MessageRole.USER).order_by(Message.id))).scalars().first()
            work = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == SESSION_ID).order_by(SessionReplyWorkItem.id))).scalars().first()
            assert message is not None
            assert message.id is not None
            assert work is not None
            assert work.id is not None
            assert work.profile_id is not None
            message_id = message.id
            work_id = work.id
            profile_id = work.profile_id

        async with factory() as db:
            session = await db.get(ChatSession, SESSION_ID)
            primary_platform = await db.get(MessagePlatform, data["platform_id"])
            assert session is not None
            assert primary_platform is not None
            assert session.message_platform_id == data["platform_id"]
            if reason == "ambiguous_legacy":
                session.message_platform_id = None
                session.message_platform_user_id = None
            elif reason == "disabled":
                primary_platform.is_enabled = False
            elif reason == "deleted":
                await db.delete(primary_platform)
            else:
                db.add(User(uid="other-owner", username="other-platform-owner"))
                primary_platform.uid = "other-owner"
            await db.commit()

        branches, _ = _patch_llm(monkeypatch)
        if stream_dispatch:
            dispatch_result = await _dispatch_queued_message(
                factory,
                work_id=work_id,
                message_id=message_id,
                profile_id=profile_id,
                stream_dispatch=True,
            )
            assert isinstance(dispatch_result, list)
            error_events = [event for event in dispatch_result if event.get("type") == "error"]
            done_events = [event for event in dispatch_result if event.get("type") == "done"]
            assert len(error_events) == 1
            assert error_events[0]["message"] == t(ERR_MESSAGE_PLATFORM_NOT_FOUND)
            assert done_events == []
        else:
            with pytest.raises(BaseBusinessException) as exc_info:
                await _dispatch_queued_message(
                    factory,
                    work_id=work_id,
                    message_id=message_id,
                    profile_id=profile_id,
                    stream_dispatch=False,
                )
            assert exc_info.value.message == ERR_MESSAGE_PLATFORM_NOT_FOUND
        assert branches == []

        async with factory() as db:
            assistant_messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID, Message.role == MessageRole.ASSISTANT))).scalars().all()
            assert assistant_messages == []

        monkeypatch.setattr(weixin_openclaw_platform_module, "AsyncSessionLocal", factory)

        async def unexpected_request(*_: Any, **__: Any) -> dict[str, Any]:
            raise AssertionError("invalid message platform source must not send an outbound request")

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", unexpected_request)
        handler = WeixinOpenClawPlatformHandler()
        assert (
            await handler.send_session_event(
                UID,
                SESSION_ID,
                "weixin-openclaw",
                {
                    "event_id": f"invalid-source-{reason}",
                    "type": "proactive_reply",
                    "content": "should-not-send",
                },
            )
            is False
        )


@pytest.mark.asyncio
async def test_weixin_openclaw_handle_message_concurrent_first_session_is_singleton(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=False,
            session_settings=_session_settings(enable_markdown=False),
        )
        _patch_runtime_database(monkeypatch, factory)

        async def no_op_title(**_: Any) -> None:
            return None

        monkeypatch.setattr(weixin_openclaw_adapter_module, "generate_session_title_for_selected_profile", no_op_title)
        error_replies: list[str] = []
        ready = asyncio.Event()
        ready_lock = asyncio.Lock()
        ready_count = 0

        async def invoke(message_text: str) -> bool:
            nonlocal ready_count
            adapter = object.__new__(WeixinOpenClawAdapter)

            async def capture_reply(user_id: str, text: str, *, context_token: str = "") -> bool:
                error_replies.append(text)
                return True

            monkeypatch.setattr(adapter, "reply_text", capture_reply)
            async with factory() as db:
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        ready.set()
                await ready.wait()
                return await adapter.handle_message(
                    db,
                    WeixinOpenClawMessage(
                        user_id=EXTERNAL_USER,
                        text=message_text,
                        session_id=SESSION_ID,
                        context_token="context-token",
                    ),
                    uid=UID,
                    message_platform_id=data["platform_id"],
                )

        results = await asyncio.gather(invoke("concurrent-one"), invoke("concurrent-two"))
        assert results == [True, True]
        assert error_replies == []

        async with factory() as db:
            sessions = (await db.execute(select(ChatSession).where(ChatSession.uid == UID, ChatSession.source == "weixin-openclaw", ChatSession.message_platform_id == data["platform_id"], ChatSession.message_platform_user_id == EXTERNAL_USER))).scalars().all()
            assert len(sessions) == 1
            session = sessions[0]

            messages = (await db.execute(select(Message).where(Message.session_id == session.session_id, Message.role == MessageRole.USER).order_by(Message.id))).scalars().all()
            assert {message.content for message in messages} == {"concurrent-one", "concurrent-two"}
            assert len(messages) == 2
            assert all(message.id is not None for message in messages)
            assert len({message.id for message in messages}) == 2

            works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session.session_id).order_by(SessionReplyWorkItem.id))).scalars().all()
            assert len(works) == 2
            assert {work.session_id for work in works} == {session.session_id}
            assert all(work.id is not None for work in works)
            assert len({work.id for work in works}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_stream", [False, True], ids=["upstream-non-stream", "upstream-stream"])
async def test_chat_session_settings_survive_next_external_message_without_affecting_sibling(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    upstream_stream: bool,
) -> None:
    _patch_external_dependencies(monkeypatch)
    async with database_factory(wal=True, foreign_keys=True) as factory:
        platform_stream = not upstream_stream
        platform_markdown = not upstream_stream
        data = await _seed_platform_data(
            factory,
            stream_llm_request=platform_stream,
            session_settings=_session_settings(enable_markdown=platform_markdown),
        )
        _patch_runtime_database(monkeypatch, factory)
        adapter = object.__new__(WeixinOpenClawAdapter)
        sibling_session_id = "opaque-sibling"

        async with factory() as db:
            created = await adapter.chat(
                db,
                "initial",
                UID,
                SESSION_ID,
                message_platform_id=data["platform_id"],
            )
            assert created.text == ""

        async with factory() as db:
            created = await adapter.chat(
                db,
                "sibling-initial",
                UID,
                sibling_session_id,
                message_platform_id=data["platform_id"],
                message_platform_user_id=EXTERNAL_USER,
            )
            assert created.text == ""

        async with factory() as db:
            sessions = (await db.execute(select(ChatSession).where(ChatSession.session_id.in_([SESSION_ID, sibling_session_id])))).scalars().all()
            assert len(sessions) == 2
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            platform_config_before = dict(platform.config)
            bindings_before = {
                session.session_id: (
                    session.source,
                    session.message_platform_id,
                    session.message_platform_user_id,
                )
                for session in sessions
            }

        app = FastAPI()
        app.include_router(chat_api.router, prefix="/api/v1")
        register_handlers(app)

        async def override_get_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(uid=UID, is_superuser=False)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            updated = await client.post(
                "/api/v1/chat/sessions/setting",
                json={
                    "session_id": SESSION_ID,
                    "stream_llm_request": upstream_stream,
                    "enable_markdown": upstream_stream,
                    "max_turns": 9,
                },
            )
            assert updated.status_code == 200
            assert updated.json()["code"] == 200

            async with factory() as db:
                received = await adapter.chat(
                    db,
                    "next-external-message",
                    UID,
                    SESSION_ID,
                    message_platform_id=data["platform_id"],
                )
                assert received.text == ""

            listed = await client.get("/api/v1/chat/sessions/list")
            assert listed.status_code == 200
            assert listed.json()["code"] == 200
            listed_by_id = {item["session_id"]: item for item in listed.json()["data"]}
            assert listed_by_id[SESSION_ID]["stream_llm_request"] is upstream_stream
            assert listed_by_id[SESSION_ID]["enable_markdown"] is upstream_stream
            assert listed_by_id[SESSION_ID]["max_turns"] == 9
            assert listed_by_id[sibling_session_id]["stream_llm_request"] is platform_stream
            assert listed_by_id[sibling_session_id]["enable_markdown"] is platform_markdown
            assert listed_by_id[sibling_session_id]["max_turns"] == 7

        async with factory() as db:
            target_session = await db.get(ChatSession, SESSION_ID)
            sibling_session = await db.get(ChatSession, sibling_session_id)
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert target_session is not None
            assert sibling_session is not None
            assert platform is not None
            assert target_session.stream_llm_request is upstream_stream
            assert target_session.enable_markdown is upstream_stream
            assert target_session.max_turns == 9
            assert sibling_session.stream_llm_request is platform_stream
            assert sibling_session.enable_markdown is platform_markdown
            assert sibling_session.max_turns == 7
            assert platform.config == platform_config_before

            bindings_after = {
                session.session_id: (
                    session.source,
                    session.message_platform_id,
                    session.message_platform_user_id,
                )
                for session in (target_session, sibling_session)
            }
            assert bindings_after == bindings_before
            assert target_session.source == "weixin-openclaw"
            assert target_session.message_platform_id == data["platform_id"]
            assert sibling_session.source == "weixin-openclaw"
            assert sibling_session.message_platform_id == data["platform_id"]
            assert sibling_session.message_platform_user_id == EXTERNAL_USER

            new_message = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == SESSION_ID,
                            Message.role == MessageRole.USER,
                            Message.content == "next-external-message",
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert new_message.id is not None
            works = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == SESSION_ID).order_by(SessionReplyWorkItem.id.desc()))).scalars().all()
            assert len(works) == 2
            new_work = works[0]
            assert new_work.id is not None
            assert new_work.profile_id is not None
            message_id = new_message.id
            work_id = new_work.id
            profile_id = new_work.profile_id

        branches, request_messages = _patch_llm(monkeypatch)
        dispatch_result = await _dispatch_queued_message(
            factory,
            work_id=work_id,
            message_id=message_id,
            profile_id=profile_id,
            stream_dispatch=False,
        )
        assert isinstance(dispatch_result, dict)
        assert branches == ["generate_with_stream_callback" if upstream_stream else "generate"]
        response_settings, _ = _extract_response_settings(request_messages)
        assert response_settings["markdown"] is upstream_stream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting_name",
    (
        "max_turns",
        "enable_markdown",
        "goal_mode",
        "show_tool_calls",
        "show_reasoning",
        "stream_llm_request",
        "profile_override_id",
        "clear_profile_override",
    ),
)
async def test_platform_setting_save_updates_only_its_sessions(database_factory, setting_name: str) -> None:
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=True,
            session_settings=_session_settings(enable_markdown=True),
        )
        second_platform_id = await _seed_second_platform(
            factory,
            profile_id=data["primary_profile_id"],
            stream_llm_request=False,
            context_token=None,
        )
        expected_platforms = {
            "platform-session-a": data["platform_id"],
            "platform-session-b": data["platform_id"],
            "unbound-session": None,
            SESSION_ID: None,
            "other-platform-session": second_platform_id,
        }
        expected_sessions = {session_id: (UID, "weixin-openclaw", platform_id) for session_id, platform_id in expected_platforms.items()}
        expected_sessions.update(
            {
                "other-owner-session": ("other-owner", "weixin-openclaw", data["platform_id"]),
                "http-session": (UID, "http", data["platform_id"]),
            }
        )
        async with factory() as db:
            db.add(User(uid="other-owner", username="settings-other-owner"))
            db.add_all(
                [
                    ChatSession(
                        session_id=session_id,
                        uid=uid,
                        profile_id=data["primary_profile_id"],
                        source=source,
                        message_platform_id=platform_id,
                        message_platform_user_id=None if session_id == SESSION_ID else EXTERNAL_USER,
                        max_turns=3,
                        enable_markdown=True,
                        goal_mode=True,
                        show_tool_calls=True,
                        show_reasoning=True,
                        stream_llm_request=True,
                        profile_override_id=data["override_profile_id"],
                    )
                    for session_id, (uid, source, platform_id) in expected_sessions.items()
                ]
            )
            await db.commit()

        app = FastAPI()
        app.include_router(message_platforms_api.router, prefix="/api/v1")
        register_handlers(app)

        async def override_get_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(uid=UID, is_superuser=False)

        initial_settings = {
            "max_turns": 3,
            "enable_markdown": True,
            "goal_mode": True,
            "show_tool_calls": True,
            "show_reasoning": True,
            "stream_llm_request": True,
            "profile_override_id": data["override_profile_id"],
        }
        new_value = {
            "max_turns": 11,
            "enable_markdown": False,
            "goal_mode": False,
            "show_tool_calls": False,
            "show_reasoning": False,
            "stream_llm_request": False,
            "profile_override_id": data["primary_profile_id"],
            "clear_profile_override": None,
        }[setting_name]
        updated_field = "profile_override_id" if setting_name == "clear_profile_override" else setting_name
        if setting_name == "stream_llm_request":
            request_payload = {"config": {"stream_llm_request": new_value}}
        elif setting_name in {"profile_override_id", "clear_profile_override"}:
            request_payload = {"profile_id": new_value}
        else:
            request_payload = {"config": {"session_settings": {setting_name: new_value}}}
        target_session_ids = {"platform-session-a", "platform-session-b", "unbound-session", SESSION_ID}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(2):
                response = await client.post(
                    "/api/v1/message-platforms/update",
                    params={"platform_id": data["platform_id"]},
                    json=request_payload,
                )
                assert response.status_code == 200
                assert response.json()["code"] == 200

                async with factory() as db:
                    for session_id, (uid, source, platform_id) in expected_sessions.items():
                        session = await db.get(ChatSession, session_id)
                        assert session is not None
                        expected_settings = initial_settings.copy()
                        if session_id in target_session_ids:
                            expected_settings[updated_field] = new_value
                        for field, expected_value in expected_settings.items():
                            assert getattr(session, field) == expected_value
                        assert session.uid == uid
                        assert session.source == source
                        assert session.message_platform_id == platform_id
                        assert session.message_platform_user_id == (None if session_id == SESSION_ID else EXTERNAL_USER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution",
    [
        "disabled_target",
        "disconnected_target",
        "ambiguous_disabled_peer",
        "no_matching_tokens",
        "matched_other_platform",
        "single_platform_fallback",
        "missing_external_user",
    ],
)
async def test_platform_setting_save_updates_only_uniquely_attributable_legacy_session(
    database_factory,
    resolution: str,
) -> None:
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=True,
            session_settings=_session_settings(enable_markdown=True),
        )
        second_platform_id = None
        if resolution not in {"single_platform_fallback", "missing_external_user"}:
            second_platform_id = await _seed_second_platform(
                factory,
                profile_id=data["primary_profile_id"],
                stream_llm_request=False,
                context_token=("second-context-token" if resolution in {"ambiguous_disabled_peer", "matched_other_platform"} else None),
            )

        session_id = "opaque-legacy-session" if resolution == "missing_external_user" else SESSION_ID
        async with factory() as db:
            primary_platform = await db.get(MessagePlatform, data["platform_id"])
            assert primary_platform is not None
            if resolution == "disabled_target":
                primary_platform.is_enabled = False
            elif resolution == "disconnected_target":
                primary_platform.status = MessagePlatformStatus.DISCONNECTED
                primary_config = dict(primary_platform.config or {})
                primary_config.pop("token", None)
                primary_platform.config = primary_config
            elif resolution == "no_matching_tokens":
                primary_platform.state = {"context_tokens": {}}
            elif resolution in {"matched_other_platform", "single_platform_fallback"}:
                primary_platform.state = {"context_tokens": {}}

            if resolution in {"ambiguous_disabled_peer", "no_matching_tokens", "matched_other_platform"}:
                assert second_platform_id is not None
                second_platform = await db.get(MessagePlatform, second_platform_id)
                assert second_platform is not None
                if resolution == "ambiguous_disabled_peer":
                    second_platform.is_enabled = False
                elif resolution == "no_matching_tokens":
                    second_platform.state = {"context_tokens": {}}

            db.add(
                ChatSession(
                    session_id=session_id,
                    uid=UID,
                    source="weixin-openclaw",
                    message_platform_id=None,
                    message_platform_user_id=None,
                    max_turns=3,
                    enable_markdown=True,
                    stream_llm_request=True,
                )
            )
            await db.commit()

        app = FastAPI()
        app.include_router(message_platforms_api.router, prefix="/api/v1")
        register_handlers(app)

        async def override_get_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(uid=UID, is_superuser=False)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": data["platform_id"]},
                json={"config": {"session_settings": {"max_turns": 11}}},
            )
            assert response.status_code == 200
            assert response.json()["code"] == 200

        async with factory() as db:
            platform = await db.get(MessagePlatform, data["platform_id"])
            session = await db.get(ChatSession, session_id)
            assert platform is not None
            assert session is not None
            assert platform.config["session_settings"]["max_turns"] == 11
            assert session.max_turns == (11 if resolution in {"disabled_target", "disconnected_target", "single_platform_fallback"} else 3)
            assert session.enable_markdown is True
            assert session.stream_llm_request is True
            assert session.source == "weixin-openclaw"
            assert session.uid == UID
            assert session.message_platform_id is None
            assert session.message_platform_user_id is None
            if resolution == "disabled_target":
                assert platform.is_enabled is False
                assert platform.status == MessagePlatformStatus.CONNECTED
            elif resolution == "disconnected_target":
                assert platform.is_enabled is True
                assert platform.status == MessagePlatformStatus.DISCONNECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("session_count", [1, 40])
@pytest.mark.parametrize("is_admin", [False, True])
async def test_session_list_resolves_platform_stream_mode_with_bounded_queries(
    database_factory,
    session_count: int,
    is_admin: bool,
) -> None:
    async with database_factory(wal=True, foreign_keys=True) as factory:
        data = await _seed_platform_data(
            factory,
            stream_llm_request=True,
            session_settings=_session_settings(enable_markdown=True),
        )
        other_uid = "other-session-list-owner"
        other_session_id = "other-owner-legacy-session"
        expected_streams: dict[str, bool] = {}

        async with factory() as db:
            db.add(User(uid=other_uid, username="session-list-other-owner"))
            db.add(
                MessagePlatform(
                    name="message-platform-session-other-owner",
                    platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                    is_enabled=True,
                    status=MessagePlatformStatus.CONNECTED,
                    uid=other_uid,
                    profile_id=data["primary_profile_id"],
                    config={"token": "other-session-list-token", "stream_llm_request": False},
                    state={"context_tokens": {EXTERNAL_USER: "other-context-token"}},
                )
            )

            sessions = []
            for index in range(session_count):
                bound_session_id = f"opaque-bound-session-{index}"
                legacy_session_id = f"opaque-legacy-session-{index}"
                sessions.extend(
                    [
                        ChatSession(
                            session_id=bound_session_id,
                            uid=UID,
                            profile_id=data["primary_profile_id"],
                            source="weixin-openclaw",
                            message_platform_id=data["platform_id"],
                            message_platform_user_id=EXTERNAL_USER,
                            stream_llm_request=None,
                        ),
                        ChatSession(
                            session_id=legacy_session_id,
                            uid=UID,
                            profile_id=data["primary_profile_id"],
                            source="weixin-openclaw",
                            message_platform_id=None,
                            message_platform_user_id=EXTERNAL_USER,
                            stream_llm_request=None,
                        ),
                    ]
                )
                expected_streams[bound_session_id] = True
                expected_streams[legacy_session_id] = True

            sessions.extend(
                [
                    ChatSession(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=data["primary_profile_id"],
                        source="weixin-openclaw",
                        message_platform_id=None,
                        message_platform_user_id=None,
                        stream_llm_request=None,
                    ),
                    ChatSession(
                        session_id="opaque-explicit-false-session",
                        uid=UID,
                        profile_id=data["primary_profile_id"],
                        source="weixin-openclaw",
                        message_platform_id=data["platform_id"],
                        message_platform_user_id=EXTERNAL_USER,
                        stream_llm_request=False,
                    ),
                    ChatSession(
                        session_id="http-null-stream-session",
                        uid=UID,
                        profile_id=data["primary_profile_id"],
                        source="http",
                        stream_llm_request=None,
                    ),
                    ChatSession(
                        session_id="ws-null-stream-session",
                        uid=UID,
                        profile_id=data["primary_profile_id"],
                        source="ws",
                        stream_llm_request=None,
                    ),
                    ChatSession(
                        session_id=other_session_id,
                        uid=other_uid,
                        profile_id=data["primary_profile_id"],
                        source="weixin-openclaw",
                        message_platform_id=None,
                        message_platform_user_id=EXTERNAL_USER,
                        stream_llm_request=None,
                    ),
                ]
            )
            db.add_all(sessions)
            await db.commit()

        expected_streams.update(
            {
                SESSION_ID: True,
                "opaque-explicit-false-session": False,
                "http-null-stream-session": False,
                "ws-null-stream-session": True,
            }
        )
        if is_admin:
            expected_streams[other_session_id] = False

        app = FastAPI()
        app.include_router(chat_api.router, prefix="/api/v1")
        register_handlers(app)

        async def override_get_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(uid=UID, is_superuser=is_admin)

        statements: list[str] = []

        def record_select(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().lower().startswith("select"):
                statements.append(statement)

        engine = factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", record_select)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/v1/chat/sessions/list")
        finally:
            event.remove(engine, "before_cursor_execute", record_select)

        assert response.status_code == 200
        assert response.json()["code"] == 200
        listed_by_id = {item["session_id"]: item for item in response.json()["data"]}
        assert {session_id: item["stream_llm_request"] for session_id, item in listed_by_id.items()} == expected_streams
        assert sum("from message_platform" in statement.lower() for statement in statements) == 1
        assert sum("from chat_session" in statement.lower() for statement in statements) == 1
