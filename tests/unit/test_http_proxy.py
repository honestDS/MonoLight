import json
from types import SimpleNamespace

import pytest

from app.api.v1 import channels as channels_module
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs, get_channel_http_proxy, normalize_http_proxy
from app.transformers.openai import OpenAIChatCompletionsTransformer
from app.transformers.openai import base as openai_base_module


class _FakeAiohttpResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self) -> str:
        return json.dumps({"data": [{"id": "gpt-test", "owned_by": "test"}]})


class _FakeClientSession:
    def __init__(self, response: _FakeAiohttpResponse):
        self._response = response
        self.get_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, "kwargs": kwargs})
        return self._response


def test_build_aiohttp_proxy_kwargs_returns_empty_dict_without_proxy() -> None:
    assert build_aiohttp_proxy_kwargs(None) == {}


def test_build_aiohttp_proxy_kwargs_omits_proxy_auth_without_credentials() -> None:
    kwargs = build_aiohttp_proxy_kwargs("http://proxy.example.com:8080")

    assert kwargs == {"proxy": "http://proxy.example.com:8080"}


def test_build_aiohttp_proxy_kwargs_decodes_credentials_and_removes_userinfo_from_proxy() -> None:
    kwargs = build_aiohttp_proxy_kwargs("http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080")

    assert kwargs["proxy"] == "http://proxy.example.com:8080"
    assert kwargs["proxy_headers"]["Proxy-Authorization"] == ("Basic dXNlckBuYW1lOnBhc3N3b3JkOndpdGgvc2xhc2g=")


def test_normalize_http_proxy_canonicalizes_encoded_credentials() -> None:
    proxy = normalize_http_proxy("http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.COM:8080/")

    assert proxy == "http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080"


@pytest.mark.parametrize(
    "channel",
    [
        SimpleNamespace(http_proxy="http://PROXY.EXAMPLE.COM:8080/"),
        {"http_proxy": "http://PROXY.EXAMPLE.COM:8080/"},
    ],
)
def test_get_channel_http_proxy_reads_top_level_object_and_dict_fields(channel: object) -> None:
    assert get_channel_http_proxy(channel) == "http://proxy.example.com:8080"


@pytest.mark.parametrize(
    "proxy",
    [
        "https://proxy.example.com:8080",
        "http://proxy.example.com",
        "http://username@proxy.example.com:8080",
        "http://:password@proxy.example.com:8080",
    ],
)
def test_normalize_http_proxy_rejects_unsupported_or_incomplete_proxies(proxy: str) -> None:
    with pytest.raises(ValueError):
        normalize_http_proxy(proxy)


@pytest.mark.asyncio
async def test_chat_completions_list_models_passes_normalized_proxy_to_fake_aiohttp_get(monkeypatch) -> None:
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(_FakeAiohttpResponse())
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    monkeypatch.setattr(openai_base_module.aiohttp, "TCPConnector", lambda **_kwargs: object())

    models = await OpenAIChatCompletionsTransformer().list_models(
        api_key="key",
        base_url="https://example.invalid/v1",
        http_proxy="http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080",
    )

    assert models == [{"id": "gpt-test", "owned_by": "test", "created": None}]
    assert len(sessions) == 1
    get_call = sessions[0].get_calls[0]
    assert get_call["url"] == "https://example.invalid/v1/models"
    assert get_call["kwargs"]["proxy"] == "http://proxy.example.com:8080"
    assert get_call["kwargs"]["proxy_headers"]["Proxy-Authorization"] == ("Basic dXNlckBuYW1lOnBhc3N3b3JkOndpdGgvc2xhc2g=")


@pytest.mark.asyncio
async def test_list_channel_models_passes_top_level_http_proxy_to_llm_client(monkeypatch) -> None:
    list_models_calls: list[dict] = []

    async def list_models(**kwargs):
        list_models_calls.append(kwargs)
        return [{"id": "gpt-test"}]

    monkeypatch.setattr(channels_module.LLMClient, "list_models", list_models)
    payload = channels_module.ChannelModelListRequest.model_validate(
        {
            "api_key": "key",
            "base_url": "https://example.invalid/v1",
            "timeout": 12,
            "http_proxy": "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.COM:8080/",
        }
    )

    await channels_module.list_channel_models(payload=payload, _admin={})

    assert list_models_calls == [
        {
            "api_key": "key",
            "base_url": "https://example.invalid/v1",
            "timeout": 12.0,
            "http_proxy": "http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080",
        }
    ]
