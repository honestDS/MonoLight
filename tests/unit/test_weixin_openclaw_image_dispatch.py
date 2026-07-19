from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.message import extract_text_and_attachments
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawChatResult


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
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_active_profile", generate_title)

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
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_active_profile", generate_title)

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
