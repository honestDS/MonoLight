import hashlib
import hmac
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import app.core.notifications.config as notification_config_module
from app.core.constants import (
    NOTIFICATION_AUTH_CONTEXT,
    NOTIFICATION_DEFAULT_HOST,
    NOTIFICATION_DEFAULT_PORT,
)
from app.core.notifications.config import NotificationConfig, load_notification_config
from app.core.system_secrets import SystemSecrets, initialize_system_secrets, load_system_secrets


@pytest.fixture
def notification_system_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SystemSecrets:
    secrets_path = tmp_path / "system_secrets.json"
    system_secrets = initialize_system_secrets(
        secrets_path,
        tmp_path / "system_secrets.lock",
        environment={},
    )

    monkeypatch.setattr(
        notification_config_module,
        "get_channel_encryption_key",
        lambda: load_system_secrets(secrets_path).channel_encryption_key,
    )
    monkeypatch.setattr(notification_config_module, "load_dotenv", lambda *_args, **_kwargs: None)
    for name in (
        "NOTIFICATION_HOST",
        "NOTIFICATION_PORT",
        "NOTIFICATION_SERVER_HOST",
        "NOTIFICATION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return system_secrets


def _set_notification_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str | None,
    port: str | None,
    server_host: str | None = None,
    token: str | None = None,
) -> None:
    for name, value in (
        ("NOTIFICATION_HOST", host),
        ("NOTIFICATION_PORT", port),
        ("NOTIFICATION_SERVER_HOST", server_host),
        ("NOTIFICATION_TOKEN", token),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("host", "port"),
    [(None, None), ("", ""), ("  ", "  ")],
    ids=["unset", "empty", "whitespace"],
)
def test_load_notification_config_uses_defaults_for_missing_or_blank_values(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    host: str | None,
    port: str | None,
) -> None:
    _set_notification_environment(monkeypatch, host=host, port=port)

    config = load_notification_config()

    assert config.host == NOTIFICATION_DEFAULT_HOST
    assert config.port == NOTIFICATION_DEFAULT_PORT


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (" 127.0.0.1 ", "127.0.0.1"),
        ("127.0.0.2", "127.0.0.2"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("192.0.2.10", "192.0.2.10"),
        ("2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
        ("0.0.0.0", "0.0.0.0"),
        ("::", "::"),
    ],
)
def test_load_notification_config_normalizes_valid_listening_ips(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    host: str,
    expected: str,
) -> None:
    _set_notification_environment(monkeypatch, host=host, port=None)

    assert load_notification_config().host == expected


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "https://127.0.0.1",
        "127.0.0.1:9000",
        "::1%scope",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_load_notification_config_rejects_invalid_listening_hosts(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    host: str,
) -> None:
    _set_notification_environment(monkeypatch, host=host, port=None)

    with pytest.raises(ValueError):
        load_notification_config()


@pytest.mark.parametrize(
    ("host", "expected"),
    [("0.0.0.0", "127.0.0.1"), ("::", "::1")],
)
def test_notification_config_connect_host_maps_wildcard_listeners_to_loopback(
    notification_system_secrets: SystemSecrets,
    host: str,
    expected: str,
) -> None:
    config = NotificationConfig(host=host, port=NOTIFICATION_DEFAULT_PORT, token="test-token")

    assert config.connect_host == expected


@pytest.mark.parametrize("server_host", [None, "", "  "], ids=["unset", "empty", "whitespace"])
def test_load_notification_config_uses_listener_fallback_for_empty_server_host(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    server_host: str | None,
) -> None:
    _set_notification_environment(
        monkeypatch,
        host="0.0.0.0",
        port=None,
        server_host=server_host,
    )

    config = load_notification_config()

    assert config.server_host is None
    assert config.connect_host == "127.0.0.1"


@pytest.mark.parametrize(
    ("host", "server_host", "expected"),
    [
        ("0.0.0.0", "192.0.2.20", "192.0.2.20"),
        ("::", "2001:db8::20", "2001:db8::20"),
    ],
)
def test_load_notification_config_prefers_remote_server_host(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    host: str,
    server_host: str,
    expected: str,
) -> None:
    _set_notification_environment(
        monkeypatch,
        host=host,
        port=None,
        server_host=server_host,
    )

    config = load_notification_config()

    assert config.host == host
    assert config.server_host == expected
    assert config.connect_host == expected


def test_load_notification_config_normalizes_remote_ipv6_host(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
) -> None:
    _set_notification_environment(
        monkeypatch,
        host="127.0.0.1",
        port=None,
        server_host="2001:0db8:0:0:0:0:0:20",
    )

    config = load_notification_config()

    assert config.server_host == "2001:db8::20"
    assert config.connect_host == "2001:db8::20"


@pytest.mark.parametrize(
    "server_host",
    [
        "0.0.0.0",
        "::",
        "localhost",
        "https://192.0.2.20",
        "192.0.2.20:9000",
        "fe80::1%scope",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_load_notification_config_rejects_invalid_remote_server_hosts(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    server_host: str,
) -> None:
    _set_notification_environment(
        monkeypatch,
        host="127.0.0.1",
        port=None,
        server_host=server_host,
    )

    with pytest.raises(ValueError):
        load_notification_config()


@pytest.mark.parametrize("port", ["0", "-1", "65536", "not-a-number", "1.5"])
def test_load_notification_config_rejects_invalid_environment_ports(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    port: str,
) -> None:
    _set_notification_environment(monkeypatch, host=None, port=port)

    with pytest.raises(ValueError):
        load_notification_config()


@pytest.mark.parametrize("port", ["1", "65535"])
def test_load_notification_config_accepts_environment_port_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    port: str,
) -> None:
    _set_notification_environment(monkeypatch, host=None, port=port)

    assert load_notification_config().port == int(port)


def test_notification_config_accepts_port_zero_for_system_assignment(
    notification_system_secrets: SystemSecrets,
) -> None:
    config = NotificationConfig(host="127.0.0.1", port=0, token="test-token")

    assert config.port == 0


@pytest.mark.parametrize("port", [True, 1.0, -1, 65536])
def test_notification_config_rejects_invalid_direct_ports(
    notification_system_secrets: SystemSecrets,
    port: object,
) -> None:
    with pytest.raises(ValueError):
        NotificationConfig(host="127.0.0.1", port=port, token="test-token")  # type: ignore[arg-type]


def test_notification_config_is_immutable_and_hides_token(
    notification_system_secrets: SystemSecrets,
) -> None:
    token = "notification-token-must-not-appear-in-repr"
    config = NotificationConfig(
        host="127.0.0.1",
        port=NOTIFICATION_DEFAULT_PORT,
        token=token,
        server_host="192.0.2.20",
    )

    assert token not in repr(config)
    with pytest.raises(FrozenInstanceError):
        config.port = 1
    with pytest.raises(FrozenInstanceError):
        config.token = "replacement-token"
    with pytest.raises(FrozenInstanceError):
        config.server_host = "192.0.2.21"


@pytest.mark.parametrize("token", [None, "", "  "], ids=["unset", "empty", "whitespace"])
def test_load_notification_config_derives_token_for_empty_overrides(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    token: str | None,
) -> None:
    _set_notification_environment(monkeypatch, host=None, port=None, token=token)

    config = load_notification_config()
    expected_token = hmac.new(
        notification_system_secrets.channel_encryption_key,
        NOTIFICATION_AUTH_CONTEXT,
        hashlib.sha256,
    ).hexdigest()

    assert config.token == expected_token


def test_load_notification_config_normalizes_explicit_token_without_reading_system_key(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
) -> None:
    token = "a" * 64
    _set_notification_environment(
        monkeypatch,
        host=None,
        port=None,
        token=f"  {token.upper()}  ",
    )

    def fail_if_system_key_is_read() -> bytes:
        pytest.fail("explicit notification token must not read system secrets")

    monkeypatch.setattr(
        notification_config_module,
        "get_channel_encryption_key",
        fail_if_system_key_is_read,
    )

    assert load_notification_config().token == token


@pytest.mark.parametrize(
    "token",
    ["invalid-notification-token", "g" * 64, "a" * 63, "a" * 65],
)
def test_load_notification_config_rejects_invalid_tokens_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
    notification_system_secrets: SystemSecrets,
    token: str,
) -> None:
    _set_notification_environment(monkeypatch, host=None, port=None, token=token)

    with pytest.raises(ValueError) as exc_info:
        load_notification_config()

    assert token not in str(exc_info.value)


def test_load_notification_config_shares_explicit_token_across_system_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    notification_system_secrets: SystemSecrets,
) -> None:
    token = "c" * 64
    _set_notification_environment(monkeypatch, host=None, port=None, token=token)
    first = load_notification_config()

    alternate_secrets_path = tmp_path / "alternate" / "system_secrets.json"
    alternate_secrets = initialize_system_secrets(
        alternate_secrets_path,
        tmp_path / "alternate" / "system_secrets.lock",
        environment={},
    )
    assert alternate_secrets.channel_encryption_key != notification_system_secrets.channel_encryption_key
    monkeypatch.setattr(
        notification_config_module,
        "get_channel_encryption_key",
        lambda: alternate_secrets.channel_encryption_key,
    )

    second = load_notification_config()

    assert first.token == second.token == token


def test_load_notification_config_derives_a_dedicated_hmac_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    notification_system_secrets: SystemSecrets,
) -> None:
    _set_notification_environment(monkeypatch, host=None, port=None)

    first = load_notification_config()
    repeated = load_notification_config()
    expected_token = hmac.new(
        notification_system_secrets.channel_encryption_key,
        NOTIFICATION_AUTH_CONTEXT,
        hashlib.sha256,
    ).hexdigest()

    assert first.token == repeated.token == expected_token
    assert len(first.token) == 64
    assert first.token != notification_system_secrets.channel_encryption_key.hex()
    assert first.token != notification_system_secrets.jwt_secret_key

    alternate_secrets_path = tmp_path / "alternate" / "system_secrets.json"
    alternate_secrets = initialize_system_secrets(
        alternate_secrets_path,
        tmp_path / "alternate" / "system_secrets.lock",
        environment={},
    )
    assert alternate_secrets.channel_encryption_key != notification_system_secrets.channel_encryption_key
    monkeypatch.setattr(
        notification_config_module,
        "get_channel_encryption_key",
        lambda: load_system_secrets(alternate_secrets_path).channel_encryption_key,
    )

    assert load_notification_config().token != first.token
