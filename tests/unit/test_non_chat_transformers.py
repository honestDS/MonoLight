import json
from email.parser import BytesParser
from email.policy import default

import pytest

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_REQUEST_TOO_LARGE,
    ERR_LLM_UNSUPPORTED_PROTOCOL,
)
from app.core.exceptions import LLMException, LLMRequestRejectedException
from app.core.utils import llm_request_policy
from app.providers.embedding.client import EmbeddingClient
from app.providers.image_generation.client import ImageGenerationClient
from app.providers.rerank.client import RerankClient
from app.transformers.cohere_rerank import CohereRerankTransformer
from app.transformers.openai import (
    OpenAIEmbeddingTransformer,
    OpenAIImageGenerationTransformer,
)
from app.transformers.openai import (
    image_generation as image_generation_module,
)
from tests.unit.response_test_support import image_generation_payload


class _FakeAiohttpResponse:
    def __init__(self, status: int, response_text: str) -> None:
        self.status = status
        self._response_text = response_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self) -> str:
        return self._response_text


class _FakeClientSession:
    def __init__(self, response: _FakeAiohttpResponse, **kwargs) -> None:
        self._response = response
        self.session_kwargs = kwargs
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "kwargs": kwargs})
        return self._response


class _CollectingWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(bytes(chunk))


@pytest.fixture(autouse=True)
def _isolate_llm_request_max_bytes(monkeypatch) -> None:
    async def load_llm_request_max_bytes() -> int:
        return 32 * 1024 * 1024

    monkeypatch.setattr(llm_request_policy, "load_llm_request_max_bytes", load_llm_request_max_bytes)


def _patch_llm_request_max_bytes(monkeypatch, max_size_bytes: int) -> None:
    async def load_llm_request_max_bytes() -> int:
        return max_size_bytes

    monkeypatch.setattr(llm_request_policy, "load_llm_request_max_bytes", load_llm_request_max_bytes)


def _patch_fake_image_http(monkeypatch, *, status: int = 200, response_text: str = '{"data": []}'):
    sessions: list[_FakeClientSession] = []
    connector_calls: list[dict] = []
    response = _FakeAiohttpResponse(status, response_text)

    def fake_client_session(**kwargs):
        session = _FakeClientSession(response, **kwargs)
        sessions.append(session)
        return session

    def fake_tcp_connector(**kwargs):
        connector_calls.append(kwargs)
        return object()

    monkeypatch.setattr(image_generation_module.aiohttp, "ClientSession", fake_client_session)
    monkeypatch.setattr(image_generation_module.aiohttp, "TCPConnector", fake_tcp_connector)
    return sessions, connector_calls


async def _collect_payload_bytes(payload) -> bytes:
    writer = _CollectingWriter()
    await payload.write(writer)
    return b"".join(writer.chunks)


async def _parse_form_data(payload) -> tuple[str, object]:
    content_type = payload.headers["Content-Type"]
    message = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + await _collect_payload_bytes(payload))
    return content_type, message


def _multipart_parts(message) -> dict[str, list]:
    parts: dict[str, list] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="Content-Disposition")
        parts.setdefault(name, []).append(part)
    return parts


def test_non_chat_clients_bind_dedicated_transformers() -> None:
    assert isinstance(EmbeddingClient.get_transformer("openai_embedding"), OpenAIEmbeddingTransformer)
    assert isinstance(ImageGenerationClient.get_transformer("openai_image"), OpenAIImageGenerationTransformer)
    assert isinstance(RerankClient.get_transformer("cohere_rerank"), CohereRerankTransformer)


@pytest.mark.parametrize(
    "client",
    [EmbeddingClient, ImageGenerationClient, RerankClient],
)
def test_non_chat_clients_reject_unsupported_protocol(client) -> None:
    with pytest.raises(LLMException) as exc_info:
        client.get_transformer("unsupported")

    assert exc_info.value.message == ERR_LLM_UNSUPPORTED_PROTOCOL


@pytest.mark.asyncio
async def test_rerank_empty_documents_returns_before_protocol_selection() -> None:
    results = await RerankClient.rerank_texts(
        api_key="test-key",
        base_url="https://example.invalid",
        model_id="test-model",
        protocol="unsupported",
        query="query",
        documents=[],
    )

    assert results == []


@pytest.mark.parametrize(
    ("base_url", "reference_images", "expected_url", "expected_file_field"),
    [
        (
            "https://example.invalid/v1",
            [],
            "https://example.invalid/v1/images/generations",
            None,
        ),
        (
            "https://example.invalid/v1/images/generations/",
            [("reference.webp", b"first-reference", "image/webp")],
            "https://example.invalid/v1/images/edits",
            "image",
        ),
        (
            "https://example.invalid/v1/images/edits/",
            [
                ("reference.webp", b"first-reference", "image/webp"),
                ("reference.png", b"second-reference\x00", "image/png"),
            ],
            "https://example.invalid/v1/images/edits",
            "image[]",
        ),
    ],
    ids=["generation", "edit-one-reference", "edit-two-references"],
)
@pytest.mark.asyncio
async def test_openai_image_references_select_json_or_multipart_transport(
    monkeypatch,
    base_url: str,
    reference_images: list[tuple[str, bytes, str]],
    expected_url: str,
    expected_file_field: str | None,
) -> None:
    sessions, connector_calls = _patch_fake_image_http(monkeypatch)

    result = await ImageGenerationClient.generate_image(
        api_key="image-api-key",
        base_url=base_url,
        model_id="gpt-image-1",
        protocol="openai_image",
        prompt="draw a blue bird",
        size="768x768",
        n=2,
        quality="high",
        response_format="b64_json",
        timeout=12.5,
        http_proxy="http://channel-proxy.example:8080",
        custom_headers={"X-Channel-Header": "image-channel"},
        reference_images=reference_images,
    )

    assert result == {"data": []}
    assert len(sessions) == 1
    assert connector_calls == [{"ssl": False}]
    session = sessions[0]
    assert session.session_kwargs["timeout"].total == 12.5
    assert len(session.post_calls) == 1

    post_call = session.post_calls[0]
    assert post_call["url"] == expected_url
    assert post_call["kwargs"]["proxy"] == "http://channel-proxy.example:8080"
    headers = post_call["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer image-api-key"
    assert headers["x-channel-header"] == "image-channel"

    if expected_file_field is None:
        expected_payload = {
            "model": "gpt-image-1",
            "prompt": "draw a blue bird",
            "n": 2,
            "size": "768x768",
            "quality": "high",
            "response_format": "b64_json",
        }
        request_data = post_call["kwargs"]["data"]
        assert isinstance(request_data, bytes)
        assert "json" not in post_call["kwargs"]
        assert request_data == json.dumps(expected_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert json.loads(request_data) == expected_payload
        assert headers["Content-Type"] == "application/json"
        return

    assert "json" not in post_call["kwargs"]
    assert "content-type" not in {key.lower() for key in headers}
    content_type, message = await _parse_form_data(post_call["kwargs"]["data"])
    assert content_type.lower().startswith("multipart/form-data;")
    assert "boundary=" in content_type.lower()
    assert message.is_multipart()

    parts = _multipart_parts(message)
    assert set(parts) == {
        "model",
        "prompt",
        "n",
        "size",
        "quality",
        "response_format",
        expected_file_field,
    }
    text_fields = {name: part.get_payload(decode=True).decode("utf-8") for name, values in parts.items() if name != expected_file_field for part in values}
    assert text_fields == {
        "model": "gpt-image-1",
        "prompt": "draw a blue bird",
        "n": "2",
        "size": "768x768",
        "quality": "high",
        "response_format": "b64_json",
    }
    assert "mask" not in parts

    file_parts = parts[expected_file_field]
    assert len(file_parts) == len(reference_images)
    for part, (filename, data, mime_type) in zip(file_parts, reference_images, strict=True):
        assert part.get_filename() == filename
        assert part.get_content_type() == mime_type
        assert part.get_payload(decode=True) == data


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://example.invalid/apple.png"),
        ("b64_json", "aGVsbG8="),
    ],
)
@pytest.mark.asyncio
async def test_openai_image_client_preserves_response_payload(
    monkeypatch,
    field: str,
    value: str,
) -> None:
    expected = {"model": "image-model", "data": [{field: value}]}
    _patch_fake_image_http(monkeypatch, response_text=json.dumps(expected))

    actual = await ImageGenerationClient.generate_image(
        api_key="key",
        base_url="https://example.invalid/v1",
        model_id="image-model",
        protocol="openai_image",
        prompt="a cat",
    )

    assert actual == expected
    assert image_generation_payload("image-model", **{field: value}) == expected


@pytest.mark.parametrize(
    "reference_images",
    [
        [],
        [("参考图一.png", b"first-reference", "image/png")],
        [
            ("参考图一.png", b"first-reference", "image/png"),
            ("参考图二.webp", b"second-reference\x00", "image/webp"),
        ],
    ],
    ids=["json", "multipart-one-reference", "multipart-many-references"],
)
@pytest.mark.parametrize("limit_delta", [-1, 0, 1], ids=["below", "equal", "above"])
@pytest.mark.asyncio
async def test_openai_image_request_size_boundaries(
    monkeypatch,
    reference_images: list[tuple[str, bytes, str]],
    limit_delta: int,
) -> None:
    sessions, connector_calls = _patch_fake_image_http(monkeypatch)
    prompt = '画一只"蓝色"鸟\\路径\n第二行'
    extra_body = {"extra_text": '普通字段"引号"\\路径\n换行'}
    request_kwargs = {
        "api_key": "image-api-key",
        "base_url": "https://example.invalid/v1",
        "model_id": "gpt-image-1",
        "protocol": "openai_image",
        "prompt": prompt,
        "size": "768x768",
        "n": 2,
        "quality": "high",
        "response_format": "b64_json",
        "extra_body": extra_body,
        "reference_images": reference_images,
    }

    await ImageGenerationClient.generate_image(**request_kwargs)
    first_request_data = sessions[0].post_calls[0]["kwargs"]["data"]
    if reference_images:
        first_wire_data = await _collect_payload_bytes(first_request_data)
        assert prompt.encode("utf-8") in first_wire_data
        assert extra_body["extra_text"].encode("utf-8") in first_wire_data
        assert all(data in first_wire_data for _, data, _ in reference_images)
        request_size_bytes = len(first_wire_data)
        assert request_size_bytes > sum(len(data) for _, data, _ in reference_images)
    else:
        expected_payload = {
            "model": "gpt-image-1",
            "prompt": prompt,
            "n": 2,
            "size": "768x768",
            "quality": "high",
            "response_format": "b64_json",
            "extra_text": extra_body["extra_text"],
        }
        assert isinstance(first_request_data, bytes)
        assert json.loads(first_request_data) == expected_payload
        assert first_request_data == json.dumps(expected_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_size_bytes = len(first_request_data)

    limit = request_size_bytes + limit_delta
    _patch_llm_request_max_bytes(monkeypatch, limit)
    sessions_before = len(sessions)
    connectors_before = len(connector_calls)
    posts_before = sum(len(session.post_calls) for session in sessions)

    if limit_delta < 0:
        with pytest.raises(LLMRequestRejectedException) as exc_info:
            await ImageGenerationClient.generate_image(**request_kwargs)

        rejection = exc_info.value
        assert rejection.message == ERR_LLM_REQUEST_TOO_LARGE
        assert rejection.source == "local"
        assert rejection.code == 400
        assert rejection.reason == "request_too_large"
        assert rejection.allow_channel_fallback is False
        assert rejection.data["request_size_bytes"] == request_size_bytes
        assert rejection.data["max_size_bytes"] == limit
        assert rejection.kwargs["request_size_bytes"] == request_size_bytes
        assert rejection.kwargs["max_size_bytes"] == limit
        assert len(sessions) == sessions_before
        assert len(connector_calls) == connectors_before
        assert sum(len(session.post_calls) for session in sessions) == posts_before
        return

    result = await ImageGenerationClient.generate_image(**request_kwargs)
    assert result == {"data": []}
    assert len(sessions) == sessions_before + 1
    assert len(connector_calls) == connectors_before + 1
    assert sum(len(session.post_calls) for session in sessions) == posts_before + 1
    sent_data = sessions[-1].post_calls[-1]["kwargs"]["data"]
    if reference_images:
        sent_size_bytes = len(await _collect_payload_bytes(sent_data))
    else:
        assert isinstance(sent_data, bytes)
        sent_size_bytes = len(sent_data)
    assert sent_size_bytes <= limit


@pytest.mark.parametrize(
    ("prompt", "extra_body", "reference_images"),
    [
        ("x" * 1100, None, []),
        ("short prompt", {"extra_text": "x" * 1100}, []),
        ("short prompt", None, [("大图.png", b"x" * 1100, "image/png")]),
        (
            "short prompt",
            None,
            [
                ("第一张.png", b"a" * 600, "image/png"),
                ("第二张.png", b"b" * 600, "image/png"),
            ],
        ),
        ("short prompt", {"extra_text": "x" * 1100}, [("短图.png", b"x", "image/png")]),
    ],
    ids=["long-json-prompt", "long-json-extra-body", "large-reference", "many-references", "long-multipart-text"],
)
@pytest.mark.asyncio
async def test_openai_image_request_size_limit_rejects_before_http(
    monkeypatch,
    prompt: str,
    extra_body: dict[str, str] | None,
    reference_images: list[tuple[str, bytes, str]],
) -> None:
    max_size_bytes = 1024
    _patch_llm_request_max_bytes(monkeypatch, max_size_bytes)
    sessions, connector_calls = _patch_fake_image_http(monkeypatch)

    with pytest.raises(LLMRequestRejectedException) as exc_info:
        await ImageGenerationClient.generate_image(
            api_key="image-api-key",
            base_url="https://example.invalid/v1",
            model_id="gpt-image-1",
            protocol="openai_image",
            prompt=prompt,
            extra_body=extra_body,
            reference_images=reference_images,
        )

    rejection = exc_info.value
    assert rejection.message == ERR_LLM_REQUEST_TOO_LARGE
    assert rejection.source == "local"
    assert rejection.code == 400
    assert rejection.reason == "request_too_large"
    assert rejection.allow_channel_fallback is False
    assert rejection.data["request_size_bytes"] > max_size_bytes
    assert rejection.data["max_size_bytes"] == max_size_bytes
    assert rejection.kwargs["request_size_bytes"] == rejection.data["request_size_bytes"]
    assert rejection.kwargs["max_size_bytes"] == max_size_bytes
    assert sessions == []
    assert connector_calls == []


@pytest.mark.parametrize(
    ("status", "response_text", "expected_message", "expected_kwargs"),
    [
        (
            400,
            "provider rejected image request",
            ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
            {"status": 400, "detail": "provider rejected image request"},
        ),
        (200, "[]", ERR_LLM_API_RESPONSE_ERROR, {}),
    ],
    ids=["http-error", "non-dict-response"],
)
@pytest.mark.asyncio
async def test_openai_image_transport_error_semantics(
    monkeypatch,
    status: int,
    response_text: str,
    expected_message: str,
    expected_kwargs: dict,
) -> None:
    _patch_fake_image_http(monkeypatch, status=status, response_text=response_text)

    with pytest.raises(LLMException) as exc_info:
        await ImageGenerationClient.generate_image(
            api_key="image-api-key",
            base_url="https://example.invalid/v1",
            model_id="gpt-image-1",
            protocol="openai_image",
            prompt="draw a blue bird",
        )

    assert exc_info.value.message == expected_message
    assert exc_info.value.kwargs == expected_kwargs


@pytest.mark.parametrize(
    "reference_images",
    [
        [],
        [("reference.png", b"reference", "image/png")],
    ],
    ids=["json-without-reference", "multipart-with-reference"],
)
@pytest.mark.asyncio
async def test_openai_image_413_rejection_preserves_upstream_detail(
    monkeypatch,
    reference_images: list[tuple[str, bytes, str]],
) -> None:
    detail = "<html><body>provider-private-413-body</body></html>"
    _patch_fake_image_http(monkeypatch, status=413, response_text=detail)

    with pytest.raises(LLMRequestRejectedException) as exc_info:
        await ImageGenerationClient.generate_image(
            api_key="image-api-key",
            base_url="https://example.invalid/v1",
            model_id="gpt-image-1",
            protocol="openai_image",
            prompt="draw a blue bird",
            reference_images=reference_images,
        )

    rejection = exc_info.value
    assert rejection.code == 502
    assert rejection.source == "upstream"
    assert rejection.upstream_status == 413
    assert rejection.upstream_detail == detail
    rendered = rejection.render_message()
    assert "413" in rendered
    assert detail not in rendered
    assert "<html>" not in rendered
    assert detail not in repr(rejection.data)


@pytest.mark.parametrize(
    ("protocol", "reference_images"),
    [
        ("openai_responses", None),
        ("OPENAI_RESPONSES", [("reference.png", b"reference", "image/png")]),
    ],
)
@pytest.mark.asyncio
async def test_openai_image_rejects_responses_protocol_before_transport(
    monkeypatch,
    protocol: str,
    reference_images: list[tuple[str, bytes, str]] | None,
) -> None:
    sessions, connector_calls = _patch_fake_image_http(monkeypatch)

    with pytest.raises(LLMException) as exc_info:
        await ImageGenerationClient.generate_image(
            api_key="image-api-key",
            base_url="https://example.invalid/v1",
            model_id="gpt-image-1",
            protocol=protocol,
            prompt="draw a blue bird",
            reference_images=reference_images,
        )

    assert exc_info.value.message == ERR_LLM_UNSUPPORTED_PROTOCOL
    assert exc_info.value.kwargs == {"protocol": protocol}
    assert sessions == []
    assert connector_calls == []
