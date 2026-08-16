import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.client import WeixinOpenClawClient
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
@pytest.mark.parametrize("use_stream_dispatch", [False, True])
async def test_platform_handler_passes_explicit_stream_dispatch_setting_to_adapter(monkeypatch, use_stream_dispatch):
    handler = WeixinOpenClawPlatformHandler()
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
        use_stream_dispatch=use_stream_dispatch,
    )

    assert captured["stream_requested"] is use_stream_dispatch


@pytest.mark.asyncio
async def test_platform_handler_reads_platform_by_id_and_resolves_stream_dispatch(monkeypatch):
    handler = WeixinOpenClawPlatformHandler()
    db = SimpleNamespace()
    platform = SimpleNamespace(use_stream_dispatch=True)
    get_calls = []

    async def get_platform(actual_db, platform_id):
        get_calls.append((actual_db, platform_id))
        return platform

    monkeypatch.setattr("app.core.message_platforms.base.message_platform_crud.get", get_platform)

    loaded_platform = await handler._get_platform_by_id(db, 7)

    assert loaded_platform is platform
    assert get_calls == [(db, 7)]
    assert handler._resolve_use_stream_dispatch(None) is False
    assert handler._resolve_use_stream_dispatch(SimpleNamespace(use_stream_dispatch=False)) is False
    assert handler._resolve_use_stream_dispatch(SimpleNamespace(use_stream_dispatch=True)) is True


@pytest.mark.asyncio
async def test_platform_handler_run_passes_platform_stream_dispatch_to_collector(monkeypatch):
    handler = WeixinOpenClawPlatformHandler()
    captured_stream_requests = []
    collector_instances = []
    platform_stream_settings = iter([True, True, False, False, False])
    session_enter_count = 0
    message = WeixinOpenClawMessage(
        user_id="weixin-user",
        text="hello",
        session_id="weixin-openclaw:weixin-user",
    )

    class FakeAdapter:
        def __init__(self):
            self.config = SimpleNamespace(poll_interval_ms=0)
            self.sync_buf = ""
            self.poll_count = 0

        async def poll_messages_once(self):
            self.poll_count += 1
            if self.poll_count <= 2:
                return [message]
            raise asyncio.CancelledError

        async def close(self):
            return None

    class FakeCollector:
        def __init__(self, *, dispatch, **_kwargs):
            self.dispatch = dispatch
            self.pending_messages = []
            collector_instances.append(self)

        async def add(self, _key, collected_message):
            self.pending_messages.append(collected_message)

        async def dispatch_next(self):
            await self.dispatch(self.pending_messages.pop(0))

        async def flush_and_wait(self, _key):
            return None

        async def close(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            nonlocal session_enter_count
            session_enter_count += 1
            if session_enter_count in {3, 5}:
                await collector_instances[0].dispatch_next()
            return SimpleNamespace()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    adapter = FakeAdapter()

    async def get_platform(_db, _platform_id):
        return SimpleNamespace(
            id=7,
            uid="owner",
            state={},
            use_stream_dispatch=next(platform_stream_settings),
        )

    async def handle_message(_adapter, _message, **kwargs):
        captured_stream_requests.append(kwargs["use_stream_dispatch"])

    monkeypatch.setattr(handler, "is_pollable", lambda current_platform: current_platform is not None)
    monkeypatch.setattr(handler, "_adapter_signature", lambda _platform: ("adapter",))
    monkeypatch.setattr(handler, "_build_adapter", lambda _platform: adapter)
    monkeypatch.setattr(handler, "_handle_message", handle_message)
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.InboundMessageCollector", FakeCollector)
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.message_platform_crud.get", get_platform)

    with pytest.raises(asyncio.CancelledError):
        await handler.run(7)

    assert captured_stream_requests == [True, False]


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
async def test_send_session_event_sends_oversized_newline_text_as_separate_requests(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    adapter.context_tokens = {"weixin-user": "context-token"}
    adapter.config = SimpleNamespace(channel_version="1", account_id="bot-account")
    request_calls = []
    first_part = "测" * 1000
    second_part = "a" * 3000

    async def request_json(*args, **kwargs):
        request_calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(adapter, "request_json", request_json)

    sent = await adapter.send_session_event(
        "owner",
        "weixin-openclaw:weixin-user",
        {"type": "proactive_reply", "content": first_part + "\n" + second_part},
    )

    assert sent is True
    assert len(request_calls) == 2
    assert all(args == ("POST", "ilink/bot/sendmessage") for args, _ in request_calls)
    payloads = [kwargs["payload"] for _, kwargs in request_calls]
    assert [payload["msg"]["item_list"] for payload in payloads] == [
        [{"type": 1, "text_item": {"text": first_part}}],
        [{"type": 1, "text_item": {"text": second_part}}],
    ]
    assert all(len(payload["msg"]["item_list"][0]["text_item"]["text"].encode("utf-8")) <= WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT for payload in payloads)
    assert payloads[0]["msg"]["client_id"] != payloads[1]["msg"]["client_id"]
    assert all(payload["msg"]["context_token"] == "context-token" for payload in payloads)


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
async def test_reply_text_parts_waits_for_each_reply_item_before_continuing(monkeypatch):
    adapter = object.__new__(WeixinOpenClawAdapter)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    call_events = []

    async def reply_items(_user_id, item_list, *, context_token=""):
        text = item_list[0]["text_item"]["text"]
        call_events.append(("start", text))
        if text == "first part":
            first_started.set()
            await release_first.wait()
        call_events.append(("complete", text))
        return True

    monkeypatch.setattr(adapter, "reply_items", reply_items)

    reply_task = asyncio.create_task(
        adapter.reply_text_parts(
            "weixin-user",
            ("first part", "second part"),
            context_token="context-token",
        )
    )

    await first_started.wait()
    assert call_events == [("start", "first part")]

    release_first.set()
    assert await reply_task is True
    assert call_events == [
        ("start", "first part"),
        ("complete", "first part"),
        ("start", "second part"),
        ("complete", "second part"),
    ]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_text", "expected_result", "expected_error"),
    [
        ('{"ret":1,"errcode":0,"errmsg":"业务失败"}', None, True),
        ('{"ret":0,"errcode":9,"errmsg":"业务失败"}', None, True),
        ('{"ret":0,"errcode":0,"data":{"ok":true}}', {"ret": 0, "errcode": 0, "data": {"ok": True}}, False),
        ('{"ret":"0","errcode":"0","data":{"ok":true}}', {"ret": "0", "errcode": "0", "data": {"ok": True}}, False),
        ('{"ret":"","errcode":"","data":{"ok":true}}', {"ret": "", "errcode": "", "data": {"ok": True}}, False),
    ],
)
async def test_request_json_handles_client_business_codes(monkeypatch, response_text, expected_result, expected_error):
    class FakeResponse:
        status = 200

        def __init__(self, text):
            self._text = text

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def text(self):
            return self._text

    class FakeSession:
        closed = False

        def __init__(self, text):
            self.response = FakeResponse(text)

        def request(self, *_args, **_kwargs):
            return self.response

    class FakeLogger:
        def __init__(self):
            self.error_messages = []

        def bind(self, **_kwargs):
            return self

        def error(self, message):
            self.error_messages.append(message)

    fake_logger = FakeLogger()
    monkeypatch.setattr("app.adapters.weixin_openclaw.client.logger", fake_logger)
    client = WeixinOpenClawClient(SimpleNamespace(base_url="https://example.test", cdn_base_url="https://example.test", token="", api_timeout_ms=1000))
    client.session = FakeSession(response_text)

    if expected_error:
        with pytest.raises(RuntimeError) as exc_info:
            await client.request_json("POST", "ilink/bot/test", token_required=False)

        assert response_text in str(exc_info.value)
        assert fake_logger.error_messages == [response_text]
    else:
        assert await client.request_json("POST", "ilink/bot/test", token_required=False) == expected_result
        assert fake_logger.error_messages == []
