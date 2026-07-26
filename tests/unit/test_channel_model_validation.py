import pytest
from pydantic import ValidationError

from app.models.channel import (
    ChannelCreate,
    ChannelModelAdvancedSettings,
    ChannelModelItem,
    ChannelResponse,
    ChannelUpdate,
    ModelProtocol,
    ModelUsage,
    normalize_channel_model_ids,
    resolve_model_protocol,
)

HTTP_PROXY_FORMAT_HINT = "仅支持 http://host:port 或 http://username:password@host:port"


@pytest.mark.parametrize("usage", list(ModelUsage))
def test_model_requires_protocol(usage: ModelUsage) -> None:
    with pytest.raises(ValidationError):
        ChannelModelItem.model_validate({"model_id": "model", "usage": usage})


@pytest.mark.parametrize(
    ("usage", "protocol"),
    [
        (ModelUsage.CHAT, ModelProtocol.OPENAI),
        (ModelUsage.CHAT, ModelProtocol.OPENAI_RESPONSES),
        (ModelUsage.EMBEDDING, ModelProtocol.OPENAI_EMBEDDING),
        (ModelUsage.RERANK, ModelProtocol.COHERE_RERANK),
        (ModelUsage.IMAGE_GENERATION, ModelProtocol.OPENAI_IMAGE),
    ],
)
def test_model_accepts_matching_protocol(usage: ModelUsage, protocol: ModelProtocol) -> None:
    model_entry = ChannelModelItem.model_validate(
        {
            "model_id": "model",
            "usage": usage,
            "protocol": protocol,
        }
    )

    assert model_entry.protocol == protocol


@pytest.mark.parametrize(
    ("http_proxy", "expected_proxy"),
    [
        ("http://proxy.example.com:8080", "http://proxy.example.com:8080"),
        (
            "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.COM:8080/",
            "http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080",
        ),
    ],
)
@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ChannelCreate, {"name": "channel", "api_key": "key"}),
        (ChannelUpdate, {}),
    ],
)
def test_channel_payload_accepts_http_proxy(schema, payload: dict, http_proxy: str, expected_proxy: str) -> None:
    channel = schema.model_validate({**payload, "http_proxy": http_proxy})

    assert channel.http_proxy == expected_proxy


@pytest.mark.parametrize("http_proxy", [None, ""])
@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ChannelCreate, {"name": "channel", "api_key": "key"}),
        (ChannelUpdate, {}),
    ],
)
def test_channel_payload_empty_http_proxy_uses_direct_connection(schema, payload: dict, http_proxy: str | None) -> None:
    channel = schema.model_validate({**payload, "http_proxy": http_proxy})

    assert channel.http_proxy is None


@pytest.mark.parametrize(
    "http_proxy",
    [
        "https://proxy.example.com:8080",
        "proxy.example.com:8080",
        "http://proxy.example.com",
        "http://username@proxy.example.com:8080",
        "http://:password@proxy.example.com:8080",
    ],
)
@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ChannelCreate, {"name": "channel", "api_key": "key"}),
        (ChannelUpdate, {}),
    ],
)
def test_channel_payload_rejects_invalid_http_proxy(schema, payload: dict, http_proxy: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        schema.model_validate({**payload, "http_proxy": http_proxy})

    assert HTTP_PROXY_FORMAT_HINT in str(exc_info.value)


def test_model_advanced_settings_preserve_future_and_legacy_proxy_fields() -> None:
    advanced_settings = {
        "future_extension": {"enabled": True, "limit": 3},
        "http_proxy": "https://legacy-proxy.example.com:not-validated",
    }

    settings = ChannelModelAdvancedSettings.model_validate(advanced_settings)
    model_ids = normalize_channel_model_ids(
        [
            {
                "model_id": "model",
                "advanced_settings": advanced_settings,
            }
        ]
    )

    assert settings.model_dump(mode="json") == advanced_settings
    assert model_ids[0]["advanced_settings"] == advanced_settings


def test_channel_response_defaults_missing_http_proxy_to_none() -> None:
    response = ChannelResponse.model_validate(
        {
            "id": 1,
            "name": "channel",
            "api_key": "key",
            "base_url": "https://example.invalid",
            "is_active": True,
            "model_ids": [],
        }
    )

    assert response.http_proxy is None


@pytest.mark.parametrize(
    ("usage", "protocol"),
    [
        (ModelUsage.CHAT, ModelProtocol.OPENAI_EMBEDDING),
        (ModelUsage.EMBEDDING, ModelProtocol.OPENAI),
        (ModelUsage.RERANK, ModelProtocol.OPENAI_IMAGE),
        (ModelUsage.IMAGE_GENERATION, ModelProtocol.COHERE_RERANK),
    ],
)
def test_model_rejects_protocol_for_different_usage(usage: ModelUsage, protocol: ModelProtocol) -> None:
    with pytest.raises(ValidationError):
        ChannelModelItem.model_validate(
            {
                "model_id": "model",
                "usage": usage,
                "protocol": protocol,
            }
        )


@pytest.mark.parametrize(
    ("protocol", "expected_protocol"),
    [
        (ModelProtocol.OPENAI, "openai"),
        (ModelProtocol.OPENAI_RESPONSES, "openai_responses"),
    ],
)
def test_resolve_model_protocol_returns_lowercase_client_key(
    protocol: ModelProtocol,
    expected_protocol: str,
) -> None:
    assert resolve_model_protocol({"protocol": protocol}) == expected_protocol
