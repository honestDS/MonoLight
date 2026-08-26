import pytest
from pydantic import ValidationError

from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    DEFAULT_CHAT_MAX_TOKENS,
    ERR_CHANNEL_MODEL_CONTEXT_WINDOW_REQUIRED,
    ERR_CHANNEL_MODEL_CUSTOM_HEADERS_INVALID,
    ERR_CHANNEL_MODEL_IDS_ITEM_INVALID,
    ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
)
from app.core.i18n import t
from app.core.utils.model_request_headers import (
    build_model_request_headers,
    get_model_custom_headers,
    normalize_model_custom_headers,
)
from app.models.channel import (
    ChannelCreate,
    ChannelModelAdvancedSettings,
    ChannelModelIdsNormalizationError,
    ChannelModelItem,
    ChannelResponse,
    ChannelUpdate,
    ModelProtocol,
    ModelUsage,
    normalize_channel_model_ids,
    resolve_model_protocol,
    validate_channel_model_ids,
)
from app.schemas.response import StandardResponse

HTTP_PROXY_FORMAT_HINT = "仅支持 http://host:port 或 http://username:password@host:port"


@pytest.mark.parametrize("api_key", ["", " ", "\t\n"])
@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ChannelCreate, {"name": "channel"}),
        (ChannelUpdate, {}),
    ],
)
def test_channel_payload_rejects_blank_api_key(schema, payload: dict, api_key: str) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "api_key": api_key})


@pytest.mark.parametrize("api_key", [" key ", "\tkey\n"])
@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ChannelCreate, {"name": "channel"}),
        (ChannelUpdate, {}),
    ],
)
def test_channel_payload_preserves_non_blank_api_key(schema, payload: dict, api_key: str) -> None:
    channel = schema.model_validate({**payload, "api_key": api_key})

    assert channel.api_key == api_key


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
    payload = {
        "model_id": "model",
        "usage": usage,
        "protocol": protocol,
    }
    if usage == ModelUsage.CHAT:
        payload["context_window_k"] = 4

    model_entry = ChannelModelItem.model_validate(payload)

    assert model_entry.protocol == protocol


@pytest.mark.parametrize("max_tokens", [744, 1000, 1001])
def test_chat_model_rejects_non_positive_input_budget(max_tokens: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChannelModelItem.model_validate(
            {
                "model_id": "model",
                "usage": ModelUsage.CHAT,
                "protocol": ModelProtocol.OPENAI,
                "context_window_k": 1,
                "max_tokens": max_tokens,
            }
        )

    expected_error = t(
        ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
        max_tokens=max_tokens,
        context_window_k=1,
        context_window_tokens=1000,
        safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    )
    assert expected_error in str(exc_info.value)


def test_chat_model_rejects_missing_context_window() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChannelModelItem.model_validate(
            {
                "model_id": "model",
                "usage": ModelUsage.CHAT,
                "protocol": ModelProtocol.OPENAI,
                "max_tokens": 4096,
            }
        )

    assert t(ERR_CHANNEL_MODEL_CONTEXT_WINDOW_REQUIRED) in str(exc_info.value)


def test_chat_model_rejects_default_max_tokens_for_small_context_window() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChannelModelItem.model_validate(
            {
                "model_id": "model",
                "usage": ModelUsage.CHAT,
                "protocol": ModelProtocol.OPENAI,
                "context_window_k": 1,
            }
        )

    expected_error = t(
        ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
        max_tokens=DEFAULT_CHAT_MAX_TOKENS,
        context_window_k=1,
        context_window_tokens=1000,
        safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    )
    assert expected_error in str(exc_info.value)


def test_validate_channel_model_ids_returns_plain_text_context_window_error() -> None:
    model_id = "gemini-3-flash"
    error_key, kwargs = validate_channel_model_ids(
        [
            {
                "model_id": model_id,
                "usage": ModelUsage.CHAT,
                "protocol": ModelProtocol.OPENAI,
                "context_window_k": 1,
                "max_tokens": 1000,
            }
        ]
    )

    expected_error = t(
        ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
        max_tokens=1000,
        context_window_k=1,
        context_window_tokens=1000,
        safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    )
    assert error_key == ERR_CHANNEL_MODEL_IDS_ITEM_INVALID
    assert kwargs["index"] == 0
    assert kwargs["model_id"] == model_id
    assert kwargs["error"] == expected_error
    assert all(str(value) in kwargs["error"] for value in (1000, 1, CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS))

    response = StandardResponse.error(code=422, message=error_key, **kwargs)

    assert response.message == t(ERR_CHANNEL_MODEL_IDS_ITEM_INVALID, **kwargs)
    assert model_id in response.message
    assert expected_error in response.message
    for forbidden in ("model_ids", "[0]"):
        assert forbidden not in response.message
    for forbidden in ("validation error for", "input_value", "input_type", "pydantic.dev", "\n"):
        assert forbidden not in response.message


@pytest.mark.parametrize(
    "model_settings",
    [
        {"context_window_k": 1, "max_tokens": 743},
        {"context_window_k": 1, "max_tokens": 0},
        {"context_window_k": 4},
    ],
)
def test_chat_model_accepts_valid_input_budget_configurations(model_settings: dict[str, int]) -> None:
    model_entry = ChannelModelItem.model_validate(
        {
            "model_id": "model",
            "usage": ModelUsage.CHAT,
            "protocol": ModelProtocol.OPENAI,
            **model_settings,
        }
    )

    assert model_entry.max_tokens == model_settings.get("max_tokens")


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
    assert settings.custom_headers == {}
    assert model_ids[0]["advanced_settings"] == advanced_settings


def test_model_advanced_settings_omits_empty_custom_headers() -> None:
    advanced_settings = {
        "user_agent": None,
        "future_extension": {"enabled": True},
    }
    expected_settings = {"future_extension": {"enabled": True}}

    settings = ChannelModelAdvancedSettings.model_validate(advanced_settings)
    model_ids = normalize_channel_model_ids(
        [
            {
                "model_id": "model",
                "advanced_settings": advanced_settings,
            }
        ]
    )

    assert settings.custom_headers == {}
    assert settings.model_dump(mode="json") == expected_settings
    assert model_ids[0]["advanced_settings"] == expected_settings


def test_model_advanced_settings_normalize_custom_headers_and_preserve_extensions() -> None:
    advanced_settings = {
        "custom_headers": {
            "User-Agent": "  MyClient/1.0  ",
            "Accept-Language": " en-US ",
        },
        "future_extension": {"enabled": True},
        "legacy_option": None,
    }
    expected_settings = {
        "custom_headers": {
            "user-agent": "MyClient/1.0",
            "accept-language": "en-US",
        },
        "future_extension": {"enabled": True},
        "legacy_option": None,
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

    assert settings.custom_headers == expected_settings["custom_headers"]
    assert settings.model_dump(mode="json") == expected_settings
    assert model_ids[0]["advanced_settings"] == expected_settings


def test_model_advanced_settings_migrates_legacy_user_agent() -> None:
    advanced_settings = {
        "user_agent": "  MyClient/1.0  ",
        "future_extension": {"enabled": True},
    }
    expected_settings = {
        "custom_headers": {"user-agent": "MyClient/1.0"},
        "future_extension": {"enabled": True},
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

    assert settings.custom_headers == {"user-agent": "MyClient/1.0"}
    assert settings.model_dump(mode="json") == expected_settings
    assert model_ids[0]["advanced_settings"] == expected_settings


@pytest.mark.parametrize(
    "custom_headers",
    [
        "not-an-object",
        {f"x-test-{index}": "value" for index in range(33)},
        {"invalid header": "value"},
        {"X-Test": "one", "x-test": "two"},
        {"authorization": "Bearer other"},
        {"Content-Type": "text/plain"},
        {"x-test": 123},
        {"x-test": ""},
        {"x-test": "   "},
        {"x-test": "value\nnext"},
        {"x-test": "value\x1fnext"},
        {"x-test": "value \u00e9"},
        {"x-test": "a" * 4097},
    ],
)
def test_normalize_model_custom_headers_rejects_invalid_values(custom_headers: object) -> None:
    with pytest.raises(ValueError):
        normalize_model_custom_headers(custom_headers)


def test_normalize_channel_model_ids_returns_plain_text_custom_headers_error() -> None:
    model_id = "gemini-3-flash"
    with pytest.raises(ChannelModelIdsNormalizationError) as exc_info:
        normalize_channel_model_ids([{"model_id": model_id, "advanced_settings": {"custom_headers": "invalid"}}])

    assert exc_info.value.index == 0
    assert exc_info.value.model_id == model_id
    assert exc_info.value.error == f"[custom_headers] {t(ERR_CHANNEL_MODEL_CUSTOM_HEADERS_INVALID)}"
    for forbidden in ("validation error for", "input_value", "input_type", "pydantic.dev", "\n"):
        assert forbidden not in exc_info.value.error


@pytest.mark.parametrize(
    ("model_entry", "expected_headers"),
    [
        (
            {
                "advanced_settings": {
                    "custom_headers": {
                        "User-Agent": "  MyClient/1.0  ",
                        "Accept-Language": " en-US ",
                    }
                }
            },
            {"user-agent": "MyClient/1.0", "accept-language": "en-US"},
        ),
        (
            {"advanced_settings": {"user_agent": "  LegacyClient/1.0  "}},
            {"user-agent": "LegacyClient/1.0"},
        ),
        (
            {
                "advanced_settings": {
                    "custom_headers": {"User-Agent": "NewClient/1.0"},
                    "user_agent": "LegacyClient/1.0",
                }
            },
            {"user-agent": "NewClient/1.0"},
        ),
        ({"advanced_settings": {"custom_headers": {"authorization": "Bearer other"}}}, {}),
        (
            {
                "advanced_settings": {
                    "custom_headers": "invalid",
                    "user_agent": "LegacyClient/1.0",
                }
            },
            {"user-agent": "LegacyClient/1.0"},
        ),
    ],
)
def test_get_model_custom_headers_handles_new_legacy_and_dirty_values(
    model_entry: dict,
    expected_headers: dict[str, str],
) -> None:
    assert get_model_custom_headers(model_entry) == expected_headers


def test_get_model_custom_headers_returns_normalized_copy() -> None:
    custom_headers = {"User-Agent": "MyClient/1.0"}
    model_entry = {"advanced_settings": {"custom_headers": custom_headers}}

    headers = get_model_custom_headers(model_entry)

    assert headers == {"user-agent": "MyClient/1.0"}
    assert headers is not custom_headers


def test_build_model_request_headers_adds_or_omits_custom_headers() -> None:
    expected_base_headers = {
        "Authorization": "Bearer key",
        "Content-Type": "application/json",
    }

    assert build_model_request_headers(
        "key",
        {
            "User-Agent": "  MyClient/1.0  ",
            "Accept-Language": " en-US ",
        },
    ) == {
        **expected_base_headers,
        "user-agent": "MyClient/1.0",
        "accept-language": "en-US",
    }
    assert build_model_request_headers("key") == expected_base_headers
    assert build_model_request_headers("key", None) == expected_base_headers
    with pytest.raises(ValueError):
        build_model_request_headers("key", {"authorization": "Bearer other"})


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
