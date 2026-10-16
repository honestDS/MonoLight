import io
import json

import pytest
from loguru import logger

from app.core.constants import ERR_LLM_REQUEST_TOO_LARGE, ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE
from app.core.exceptions import LLMRequestRejectedException, find_llm_request_rejection
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request

_HISTORICAL_IMAGE_DATA_URLS = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD",
    "data:image/webp;base64,UklGRiIAAABXRUJQVlA4TAYAAAAvAAAA",
)
_LONG_ARGUMENT = "长参数-" + ("中文参数/" * 80)
_TOOL_DESCRIPTION = "用于检索历史资料的工具说明：" + ("详细说明 " * 32)
_ESCAPE_TEXT = '中文文本："引号"；反斜杠\\；换行\n；制表\t；斜线/。'


@pytest.fixture
def rich_llm_payload():
    return {
        "model": "中文模型",
        "messages": [
            {"role": "user", "content": _ESCAPE_TEXT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "第一段历史消息，包含第一张图片"},
                    {"type": "image_url", "image_url": {"url": _HISTORICAL_IMAGE_DATA_URLS[0]}},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "第二段历史消息，包含第二张图片"},
                    {"type": "image_url", "image_url": {"url": _HISTORICAL_IMAGE_DATA_URLS[1]}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "第三段历史消息，包含第三张图片"},
                    {"type": "image_url", "image_url": {"url": _HISTORICAL_IMAGE_DATA_URLS[2]}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_history_search",
                        "type": "function",
                        "function": {
                            "name": "search_history",
                            "arguments": json.dumps(
                                {"query": _LONG_ARGUMENT, "scope": "历史知识", "include_archived": True},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": _TOOL_DESCRIPTION,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索词"},
                            "scope": {"type": "string", "enum": ["历史知识", "当前会话"]},
                            "include_archived": {"type": "boolean"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }


@pytest.fixture
def chinese_locale():
    token = set_current_locale("zh")
    try:
        yield
    finally:
        reset_current_locale(token)


def _make_rejection(*, source: str, allow_channel_fallback: bool) -> LLMRequestRejectedException:
    local_message_params = {}
    if not allow_channel_fallback:
        local_message_params = {
            "request_size_bytes": 1,
            "max_size_bytes": 2,
            "request_size_mib": "0.00",
            "max_size_mib": "0.00",
        }

    return LLMRequestRejectedException(
        message=ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE if allow_channel_fallback else ERR_LLM_REQUEST_TOO_LARGE,
        source=source,
        reason="request_too_large",
        allow_channel_fallback=allow_channel_fallback,
        code=502 if allow_channel_fallback else 400,
        **local_message_params,
    )


def test_serialize_llm_json_request_returns_exact_utf8_json_at_limit(rich_llm_payload):
    expected = json.dumps(rich_llm_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    serialized = serialize_llm_json_request(rich_llm_payload, max_size_bytes=len(expected))

    assert serialized == expected
    assert json.loads(serialized) == rich_llm_payload
    assert "中文文本".encode() in serialized
    assert b"\\u4e2d" not in serialized
    assert b'\\"' in serialized
    assert b"\\\\" in serialized
    assert b"\\n" in serialized
    assert b"\\t" in serialized
    for image_data_url in _HISTORICAL_IMAGE_DATA_URLS:
        assert image_data_url.encode("utf-8") in serialized
    assert _TOOL_DESCRIPTION.encode("utf-8") in serialized
    assert _LONG_ARGUMENT.encode("utf-8") in serialized


@pytest.mark.parametrize("extra_capacity", [1, 128])
def test_serialize_llm_json_request_allows_payload_below_limit(rich_llm_payload, extra_capacity):
    expected = json.dumps(rich_llm_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    serialized = serialize_llm_json_request(rich_llm_payload, max_size_bytes=len(expected) + extra_capacity)

    assert json.loads(serialized) == rich_llm_payload


def test_serialize_llm_json_request_rejects_one_byte_over_limit(rich_llm_payload, chinese_locale):
    expected = json.dumps(rich_llm_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    limit = len(expected) - 1

    with pytest.raises(LLMRequestRejectedException) as exc_info:
        serialize_llm_json_request(rich_llm_payload, max_size_bytes=limit)

    rejection = exc_info.value
    assert rejection.code == 400
    assert rejection.source == "local"
    assert rejection.reason == "request_too_large"
    assert rejection.allow_channel_fallback is False
    assert rejection.data["request_size_bytes"] == len(expected)
    assert rejection.data["max_size_bytes"] == limit

    rendered = rejection.render_message()
    assert str(len(expected)) in rendered
    assert str(limit) in rendered
    assert "已在本地拦截，本次调用未发送" in rendered
    assert all(image_data_url not in rendered for image_data_url in _HISTORICAL_IMAGE_DATA_URLS)
    assert all(image_data_url not in repr(rejection.data) for image_data_url in _HISTORICAL_IMAGE_DATA_URLS)
    assert _LONG_ARGUMENT not in rendered


@pytest.mark.parametrize("status", [400, 429, 500, None])
def test_build_upstream_rejection_ignores_non_413_statuses(status):
    assert build_upstream_rejection(status=status, detail={"status": status}) is None


def test_build_upstream_rejection_413_preserves_detail_and_logs_raw_body(chinese_locale):
    detail = '<html><body>{"error":"request too large"}</body></html>'
    log_stream = io.StringIO()
    sink_id = logger.add(log_stream, level="WARNING", format="{message}")
    try:
        rejection = build_upstream_rejection(status=413, detail=detail)
    finally:
        logger.remove(sink_id)

    assert rejection is not None
    assert rejection.source == "upstream"
    assert rejection.reason == "request_too_large"
    assert rejection.code == 502
    assert rejection.allow_channel_fallback is True
    assert rejection.upstream_status == 413
    assert rejection.upstream_detail == detail

    assert "413" in log_stream.getvalue()
    assert detail in log_stream.getvalue()

    rendered = rejection.render_message()
    assert detail not in rendered
    assert "<html>" not in rendered
    assert '{"error":"request too large"}' not in rendered


def test_find_llm_request_rejection_walks_nested_groups_and_wrapped_cause():
    rejection = _make_rejection(source="local", allow_channel_fallback=False)
    wrapped = RuntimeError("request handling failed")
    wrapped.__cause__ = rejection
    nested = ExceptionGroup("inner failures", [ValueError("unrelated"), wrapped])
    grouped = ExceptionGroup("outer failures", [TypeError("other"), nested])

    assert find_llm_request_rejection(grouped) is rejection


def test_find_llm_request_rejection_prefers_local_rejection_over_fallback():
    fallback = _make_rejection(source="upstream", allow_channel_fallback=True)
    local = _make_rejection(source="local", allow_channel_fallback=False)
    wrapped_local = RuntimeError("local validation failed")
    wrapped_local.__cause__ = local
    grouped = ExceptionGroup("mixed failures", [fallback, wrapped_local])

    assert find_llm_request_rejection(grouped) is local


def test_find_llm_request_rejection_ignores_unrelated_errors_and_cause_cycles():
    unrelated = ExceptionGroup("unrelated failures", [ValueError("one"), KeyError("two")])
    assert find_llm_request_rejection(unrelated) is None

    first = RuntimeError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert find_llm_request_rejection(first) is None
