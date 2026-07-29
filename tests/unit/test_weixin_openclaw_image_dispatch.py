from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.constants import (
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_ASCII_CHAR_LIMIT,
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_CHINESE_CHAR_LIMIT,
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
)
from app.adapters.weixin_openclaw.message import extract_text_and_attachments
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawChatResult, WeixinOpenClawMessage
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler
from app.core.prompts import WEIXIN_OPENCLAW_CONCISE_OUTPUT_SYSTEM_PROMPT


class InboundMediaAdapter:
    async def resolve_inbound_image(self, item):
        return Path("temp/weixin_openclaw/inbound.jpg")

    async def resolve_inbound_file(self, item):
        return None


@pytest.mark.asyncio
async def test_text_item_keeps_referenced_image_attachment():
    item_list = [
        {
            "type": 1,
            "text_item": {"text": "测试一下你的识图能力"},
            "ref_msg": {
                "message_item": {
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": "encrypted-image",
                        }
                    },
                }
            },
        }
    ]

    text, attachments = await extract_text_and_attachments(InboundMediaAdapter(), item_list)

    expected_attachment = str(Path("temp/weixin_openclaw/inbound.jpg"))
    assert text == "测试一下你的识图能力"
    assert attachments == [expected_attachment]


@pytest.mark.asyncio
async def test_converted_referenced_image_reaches_chat_dispatch(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    adapter.context_tokens = {}
    captured = {}
    title_calls = []

    async def resolve_inbound_image(item):
        return Path("temp/weixin_openclaw/inbound.jpg")

    async def chat(**kwargs):
        captured.update(kwargs)
        return WeixinOpenClawChatResult()

    async def generate_title(**kwargs):
        title_calls.append(kwargs)
        return None

    monkeypatch.setattr(adapter, "resolve_inbound_image", resolve_inbound_image)
    monkeypatch.setattr(adapter, "chat", chat)
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_selected_profile", generate_title)

    converted = await adapter.convert_message(
        {
            "from_user_id": "weixin-user",
            "context_token": "context-token",
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": "测试一下你的识图能力"},
                    "ref_msg": {
                        "message_item": {
                            "type": 2,
                            "image_item": {
                                "media": {
                                    "encrypt_query_param": "encrypted-image",
                                }
                            },
                        }
                    },
                }
            ],
        }
    )

    expected_attachment = str(Path("temp/weixin_openclaw/inbound.jpg"))
    assert converted is not None
    assert converted.attachments == [expected_attachment]

    handled = await adapter.handle_message(
        SimpleNamespace(),
        converted,
        uid="owner",
    )

    assert handled is True
    assert captured["message"] == "测试一下你的识图能力"
    assert captured["attachments"] == [expected_attachment]
    assert title_calls[0]["first_message"] == captured["message"]


@pytest.mark.asyncio
async def test_failed_chat_does_not_generate_title(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    adapter.context_tokens = {}
    reply_calls = []
    title_calls = []

    async def chat(**kwargs):
        return WeixinOpenClawChatResult(text="消息入队失败")

    async def reply_text(user_id, text, *, context_token=""):
        reply_calls.append((user_id, text, context_token))
        return True

    async def generate_title(**kwargs):
        title_calls.append(kwargs)
        return None

    monkeypatch.setattr(adapter, "chat", chat)
    monkeypatch.setattr(adapter, "reply_text", reply_text)
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_selected_profile", generate_title)

    handled = await adapter.handle_message(
        SimpleNamespace(),
        SimpleNamespace(
            user_id="weixin-user",
            text="请帮我分析这个问题",
            session_id="weixin-openclaw:weixin-user",
            context_token="context-token",
            attachments=[],
        ),
        uid="owner",
    )

    assert handled is True
    assert reply_calls == [("weixin-user", "消息入队失败", "context-token")]
    assert title_calls == []


@pytest.mark.asyncio
async def test_unsupported_session_event_is_ignored_without_reply(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    reply_calls = []

    async def reply_text(*args, **kwargs):
        reply_calls.append((args, kwargs))
        return True

    async def reply_file_item(*args, **kwargs):
        reply_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(adapter, "reply_text", reply_text)
    monkeypatch.setattr(adapter, "reply_file_item", reply_file_item)

    sent = await adapter.send_session_event(
        "owner",
        "weixin-openclaw:weixin-user",
        {"type": "audit_confirmation_status", "status": "pending"},
    )

    assert sent is True
    assert reply_calls == []


@pytest.mark.asyncio
async def test_chat_passes_concise_system_prompt_to_preflight_and_queue(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    profile = SimpleNamespace(id=1, uid="owner")
    captured = {}

    async def resolve_profile(db, *, uid, session_id, message_platform_id=None):
        assert uid == "owner"
        assert message_platform_id == 7
        return profile

    async def validate_initial_message_before_save(*args, **kwargs):
        captured["preflight_args"] = args
        captured["preflight_kwargs"] = kwargs

    async def submit_user_message(*args, **kwargs):
        captured["submit_kwargs"] = kwargs

    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr(
        "app.adapters.weixin_openclaw.adapter.ChatDispatcher.validate_initial_message_before_save",
        validate_initial_message_before_save,
    )
    monkeypatch.setattr(
        "app.adapters.weixin_openclaw.adapter.session_reply_queue_manager.submit_user_message",
        submit_user_message,
    )

    result = await adapter.chat(
        SimpleNamespace(),
        "hello",
        "owner",
        "weixin-openclaw:weixin-user",
        message_platform_id=7,
    )

    expected_system_prompt = WEIXIN_OPENCLAW_CONCISE_OUTPUT_SYSTEM_PROMPT.format(
        chinese_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_CHINESE_CHAR_LIMIT,
        ascii_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_ASCII_CHAR_LIMIT,
        utf8_byte_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
    )
    assert result == WeixinOpenClawChatResult()
    assert "1000" in expected_system_prompt
    assert "3000" in expected_system_prompt
    assert captured["preflight_kwargs"]["additional_system_prompt"] == expected_system_prompt
    assert captured["submit_kwargs"]["additional_system_prompt"] == expected_system_prompt
    assert captured["submit_kwargs"]["stream_requested"] is False


@pytest.mark.asyncio
async def test_chat_passes_explicit_stream_request_to_queue(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    captured = {}

    async def resolve_profile(*args, **kwargs):
        return SimpleNamespace(id=1, uid="owner")

    async def validate_initial_message_before_save(*args, **kwargs):
        return None

    async def submit_user_message(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr(
        "app.adapters.weixin_openclaw.adapter.ChatDispatcher.validate_initial_message_before_save",
        validate_initial_message_before_save,
    )
    monkeypatch.setattr(
        "app.adapters.weixin_openclaw.adapter.session_reply_queue_manager.submit_user_message",
        submit_user_message,
    )

    result = await adapter.chat(
        SimpleNamespace(),
        "hello",
        "owner",
        "weixin-openclaw:weixin-user",
        stream_requested=True,
    )

    assert result == WeixinOpenClawChatResult()
    assert captured["stream_requested"] is True


@pytest.mark.asyncio
async def test_platform_handler_passes_stream_dispatch_setting_to_adapter(monkeypatch):
    handler = WeixinOpenClawPlatformHandler()
    assert handler.use_stream_dispatch is True
    captured = {}

    class FakeAdapter:
        async def handle_message(self, *args, **kwargs):
            captured.update(kwargs)

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    async def save_context_token(*args, **kwargs):
        return None

    async def is_current_adapter(*args, **kwargs):
        return True

    monkeypatch.setattr(handler, "_save_context_token", save_context_token)
    monkeypatch.setattr(handler, "_is_current_adapter", is_current_adapter)
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.AsyncSessionLocal", lambda: SessionContext())

    await handler._handle_message(
        FakeAdapter(),
        WeixinOpenClawMessage(
            user_id="weixin-user",
            text="hello",
            session_id="weixin-openclaw:weixin-user",
        ),
        uid="owner",
        platform_id=7,
        adapter_signature=("adapter",),
    )

    assert captured["stream_requested"] is True


@pytest.mark.asyncio
async def test_send_session_event_sends_normal_short_text(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    reply_calls = []

    async def reply_text(user_id, text, *, context_token=""):
        reply_calls.append((user_id, text, context_token))
        return True

    monkeypatch.setattr(adapter, "reply_text", reply_text)

    sent = await adapter.send_session_event(
        "owner",
        "weixin-openclaw:weixin-user",
        {"type": "proactive_reply", "content": "short reply"},
    )

    assert sent is True
    assert reply_calls == [("weixin-user", "short reply", "")]


@pytest.mark.asyncio
async def test_send_session_event_sends_oversized_newline_text_as_two_items(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    reply_calls = []
    first_part = "测" * 1000
    second_part = "a" * 3000

    async def reply_items(user_id, item_list, *, context_token=""):
        reply_calls.append((user_id, item_list, context_token))
        return True

    monkeypatch.setattr(adapter, "reply_items", reply_items)

    sent = await adapter.send_session_event(
        "owner",
        "weixin-openclaw:weixin-user",
        {"type": "proactive_reply", "content": first_part + "\n" + second_part},
    )

    assert sent is True
    assert len(reply_calls) == 1
    assert reply_calls[0][0] == "weixin-user"
    assert reply_calls[0][2] == ""
    assert [item["text_item"]["text"] for item in reply_calls[0][1]] == [first_part, second_part]
    assert all(len(item["text_item"]["text"].encode("utf-8")) <= WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT for item in reply_calls[0][1])


@pytest.mark.asyncio
async def test_send_session_event_returns_false_for_oversized_text(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)

    async def reply_items(*args, **kwargs):
        raise AssertionError("reply_items must not be called for oversized text")

    monkeypatch.setattr(adapter, "reply_items", reply_items)

    sent = await adapter.send_session_event(
        "owner",
        "weixin-openclaw:weixin-user",
        {"type": "proactive_reply_error", "content": "测" * 1001},
    )

    assert sent is False


@pytest.mark.asyncio
async def test_reply_text_rejects_oversized_text_without_sending_items(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)

    async def reply_items(*args, **kwargs):
        raise AssertionError("reply_items must not be called for oversized text")

    monkeypatch.setattr(adapter, "reply_items", reply_items)

    sent = await adapter.reply_text("weixin-user", "测" * 1001)

    assert sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_sent"),
    [
        ("测" * 1000, True),
        ("测" * 1001, False),
        ("a" * 3000, True),
        ("a" * 3001, False),
        ("测" * 999 + "abc", True),
        ("测" * 999 + "abcd", False),
    ],
    ids=[
        "chinese-at-limit",
        "chinese-over-limit",
        "ascii-at-limit",
        "ascii-over-limit",
        "mixed-at-limit",
        "mixed-over-limit",
    ],
)
async def test_reply_text_enforces_utf8_byte_limit(monkeypatch, text, expected_sent):
    adapter = object.__new__(WeixinOpenClawAdapter)
    reply_calls = []

    async def reply_items(*args, **kwargs):
        reply_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(adapter, "reply_items", reply_items)

    sent = await adapter.reply_text("weixin-user", text)

    assert sent is expected_sent
    assert bool(reply_calls) is expected_sent
