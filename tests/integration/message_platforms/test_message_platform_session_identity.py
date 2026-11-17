from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import select

import app.adapters.weixin_openclaw.adapter as weixin_openclaw_adapter_module
import app.core.message_platforms.manager as message_platform_manager_module
import app.core.message_platforms.notifier as message_platform_notifier_module
import app.core.message_platforms.weixin_openclaw as weixin_openclaw_platform_module
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.core.constants import (
    ERR_MESSAGE_PLATFORM_NOT_FOUND,
    ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED,
    ERR_SESSION_NO_PERMISSION,
)
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler
from app.models.message import Message, MessageRole
from app.models.message_platform import MessagePlatform, MessagePlatformStatus
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from tests.integration.message_platforms.test_message_platform_session_settings import (
    EXTERNAL_USER,
    FINAL_TEXT,
    SESSION_ID,
    UID,
    _dispatch_queued_message,
    _patch_external_dependencies,
    _patch_llm,
    _patch_runtime_database,
    _seed_platform_data,
    _seed_second_platform,
    _session_settings,
)


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
@pytest.mark.parametrize(
    "change",
    ["none", "disable", "waiting-login", "token-changed", "owner-changed", "deleted", "qrcode-expired"],
)
async def test_weixin_openclaw_collected_messages_survive_poll_error_without_cross_account_dispatch(
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
        _patch_runtime_database(monkeypatch, factory)
        monkeypatch.setattr(weixin_openclaw_platform_module, "AsyncSessionLocal", factory)

        async with factory() as db:
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            platform.config = {
                **(platform.config or {}),
                "poll_interval_ms": 0,
            }
            await db.commit()

        monkeypatch.setattr(weixin_openclaw_platform_module, "INBOUND_COLLECTION_QUIET_PERIOD_SECONDS", 0.01)
        monkeypatch.setattr(weixin_openclaw_platform_module, "INBOUND_COLLECTION_MAX_WAIT_SECONDS", 0.02)

        async def no_op_title(**_: Any) -> None:
            return None

        monkeypatch.setattr(weixin_openclaw_adapter_module, "generate_session_title_for_selected_profile", no_op_title)

        request_count = 0

        async def get_updates(
            _self: WeixinOpenClawAdapter,
            _method: str,
            endpoint: str,
            **_: Any,
        ) -> dict[str, Any]:
            nonlocal request_count
            assert "getupdates" in endpoint.lower()
            request_count += 1
            if request_count == 2:
                raise ConnectionError("offline")
            assert request_count == 1
            return {
                "ret": 0,
                "get_updates_buf": "next-sync",
                "msgs": [
                    {
                        "from_user_id": EXTERNAL_USER,
                        "to_user_id": "bot",
                        "client_id": "collected-message-1",
                        "message_id": "collected-message-1",
                        "message_type": 1,
                        "message_state": 2,
                        "context_token": "new-context-token",
                        "item_list": [{"type": 1, "text_item": {"text": "collected-one"}}],
                    },
                    {
                        "from_user_id": EXTERNAL_USER,
                        "to_user_id": "bot",
                        "client_id": "collected-message-2",
                        "message_id": "collected-message-2",
                        "message_type": 1,
                        "message_state": 2,
                        "context_token": "new-context-token",
                        "item_list": [{"type": 1, "text_item": {"text": "collected-two"}}],
                    },
                ],
            }

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", get_updates)

        release_dispatch = asyncio.Event()
        done = asyncio.Event()
        handler = WeixinOpenClawPlatformHandler()
        original_handle_message = handler._handle_message

        async def observe_handle_message(*args: Any, **kwargs: Any) -> Any:
            try:
                await release_dispatch.wait()
                return await original_handle_message(*args, **kwargs)
            finally:
                done.set()

        monkeypatch.setattr(handler, "_handle_message", observe_handle_message)

        async def controlled_sleep(seconds: float) -> None:
            if seconds != 30:
                await asyncio.sleep(seconds)
                return

            async with factory() as db:
                platform = await db.get(MessagePlatform, data["platform_id"])
                assert platform is not None
                assert platform.status == MessagePlatformStatus.ERROR
                assert platform.last_error == "offline"

                if change == "disable":
                    platform.is_enabled = False
                elif change == "waiting-login":
                    platform.status = MessagePlatformStatus.WAITING_LOGIN
                elif change == "token-changed":
                    platform.config = {**(platform.config or {}), "token": "replacement-session-token"}
                elif change == "owner-changed":
                    db.add(User(uid="other-owner", username="collector-other-owner"))
                    await db.flush()
                    platform.uid = "other-owner"
                elif change == "deleted":
                    await db.delete(platform)
                elif change == "qrcode-expired":
                    platform.status = MessagePlatformStatus.ERROR
                    platform.last_error = ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED

                await db.commit()

            release_dispatch.set()
            await asyncio.wait_for(done.wait(), timeout=2)
            raise asyncio.CancelledError

        monkeypatch.setattr(
            weixin_openclaw_platform_module,
            "asyncio",
            SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=controlled_sleep),
        )

        run_task = asyncio.create_task(handler.run(data["platform_id"]))
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, timeout=2)
        finally:
            release_dispatch.set()
            if not run_task.done():
                run_task.cancel()
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=2)

        assert request_count == 2
        async with factory() as db:
            sessions = (await db.execute(select(ChatSession))).scalars().all()
            messages = (await db.execute(select(Message))).scalars().all()
            works = (await db.execute(select(SessionReplyWorkItem))).scalars().all()

            if change == "none":
                assert len(sessions) == 1
                session = sessions[0]
                assert session.uid == UID
                assert session.source == "weixin-openclaw"
                assert session.message_platform_id == data["platform_id"]
                assert session.message_platform_user_id == EXTERNAL_USER

                assert len(messages) == 1
                message = messages[0]
                assert message.uid == UID
                assert message.role == MessageRole.USER
                assert message.content == "collected-one\ncollected-two"
                assert message.session_id == session.session_id

                assert len(works) == 1
                work = works[0]
                assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert work.uid == UID
                assert work.session_id == session.session_id
                assert work.source_id == str(message.id)
                assert work.source_type == "user_message"

                platform = await db.get(MessagePlatform, data["platform_id"])
                assert platform is not None
                assert platform.status == MessagePlatformStatus.ERROR
                assert platform.last_error == "offline"
                assert platform.state["sync_buf"] == "next-sync"
                assert platform.state["context_tokens"][EXTERNAL_USER] == "new-context-token"
            else:
                assert sessions == []
                assert messages == []
                assert works == []

                platform = await db.get(MessagePlatform, data["platform_id"])
                if change == "deleted":
                    assert platform is None
                else:
                    assert platform is not None
                    assert platform.state["context_tokens"][EXTERNAL_USER] == "context-token"
                    if change == "disable":
                        assert platform.is_enabled is False
                        assert platform.status == MessagePlatformStatus.ERROR
                        assert platform.last_error == "offline"
                    elif change == "waiting-login":
                        assert platform.status == MessagePlatformStatus.WAITING_LOGIN
                        assert platform.last_error == "offline"
                    elif change == "token-changed":
                        assert platform.get_config_secret("token") == "replacement-session-token"
                        assert platform.status == MessagePlatformStatus.ERROR
                        assert platform.last_error == "offline"
                    elif change == "owner-changed":
                        assert platform.uid == "other-owner"
                        assert platform.status == MessagePlatformStatus.ERROR
                        assert platform.last_error == "offline"
                    else:
                        assert change == "qrcode-expired"
                        assert platform.status == MessagePlatformStatus.ERROR
                        assert platform.last_error == ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED


@pytest.mark.asyncio
async def test_weixin_openclaw_buffered_message_reuses_unbound_legacy_session_after_poll_error(
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
        monkeypatch.setattr(weixin_openclaw_platform_module, "AsyncSessionLocal", factory)

        async with factory() as db:
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            platform.status = MessagePlatformStatus.ERROR
            platform.last_error = "offline"
            db.add(
                ChatSession(
                    session_id=SESSION_ID,
                    uid=UID,
                    source="weixin-openclaw",
                    profile_id=data["primary_profile_id"],
                )
            )
            await db.commit()

        async def no_op_title(**_: Any) -> None:
            return None

        async def unexpected_request(*_: Any, **__: Any) -> dict[str, Any]:
            raise AssertionError("buffered message handling must not make a network request")

        monkeypatch.setattr(weixin_openclaw_adapter_module, "generate_session_title_for_selected_profile", no_op_title)
        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", unexpected_request)

        handler = WeixinOpenClawPlatformHandler()
        async with factory() as db:
            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            adapter = handler._build_adapter(platform)
            adapter_signature = handler._adapter_signature(platform)

        try:
            await handler._handle_message(
                adapter,
                WeixinOpenClawMessage(
                    user_id=EXTERNAL_USER,
                    text="buffered-legacy",
                    session_id=SESSION_ID,
                    context_token="updated-context",
                ),
                uid=UID,
                platform_id=data["platform_id"],
                adapter_signature=adapter_signature,
                use_stream_dispatch=False,
            )
        finally:
            await adapter.close()

        async with factory() as db:
            sessions = (await db.execute(select(ChatSession))).scalars().all()
            messages = (await db.execute(select(Message))).scalars().all()
            works = (await db.execute(select(SessionReplyWorkItem))).scalars().all()

            assert len(sessions) == 1
            session = sessions[0]
            assert session.session_id == SESSION_ID
            assert session.uid == UID
            assert session.source == "weixin-openclaw"
            assert session.message_platform_id == data["platform_id"]
            assert session.message_platform_user_id == EXTERNAL_USER

            assert len(messages) == 1
            message = messages[0]
            assert message.session_id == SESSION_ID
            assert message.uid == UID
            assert message.role == MessageRole.USER
            assert message.content == "buffered-legacy"

            assert len(works) == 1
            work = works[0]
            assert work.session_id == SESSION_ID
            assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert work.source_type == "user_message"
            assert work.source_id == str(message.id)

            platform = await db.get(MessagePlatform, data["platform_id"])
            assert platform is not None
            assert platform.status == MessagePlatformStatus.ERROR
            assert platform.last_error == "offline"
            assert platform.state["context_tokens"][EXTERNAL_USER] == "updated-context"
