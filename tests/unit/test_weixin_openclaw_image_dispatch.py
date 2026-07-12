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

    async def resolve_inbound_image(item):
        return Path("temp/weixin_openclaw/inbound.jpg")

    async def chat(**kwargs):
        captured.update(kwargs)
        return WeixinOpenClawChatResult()

    async def generate_title(**kwargs):
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
