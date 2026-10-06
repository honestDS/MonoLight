from email.parser import BytesParser
from email.policy import default

import pytest

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_UNSUPPORTED_PROTOCOL,
)
from app.core.exceptions import LLMException
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


async def _parse_form_data(form) -> tuple[str, object]:
    payload = form()
    writer = _CollectingWriter()
    await payload.write(writer)
    content_type = payload.headers["Content-Type"]
    message = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + b"".join(writer.chunks))
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
        assert post_call["kwargs"]["json"] == {
            "model": "gpt-image-1",
            "prompt": "draw a blue bird",
            "n": 2,
            "size": "768x768",
            "quality": "high",
            "response_format": "b64_json",
        }
        assert "data" not in post_call["kwargs"]
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
