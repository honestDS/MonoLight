"""Configuration for a local or shared TCP notification center."""

import hashlib
import hmac
import ipaddress
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from app.core.constants import (
    ERR_NOTIFICATION_HOST_INVALID,
    ERR_NOTIFICATION_PORT_INVALID,
    ERR_NOTIFICATION_SERVER_HOST_INVALID,
    ERR_NOTIFICATION_TOKEN_INVALID,
    NOTIFICATION_AUTH_CONTEXT,
    NOTIFICATION_DEFAULT_HOST,
    NOTIFICATION_DEFAULT_PORT,
)
from app.core.i18n import t
from app.core.system_secrets import get_channel_encryption_key


def _normalize_ip_address(
    value: str,
    error_key: str,
    allow_unspecified: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(t(error_key))

    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        raise ValueError(t(error_key)) from None

    if getattr(address, "scope_id", None) is not None or address.is_multicast:
        raise ValueError(t(error_key))

    if not allow_unspecified and address.is_unspecified:
        raise ValueError(t(error_key))

    return str(address)


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    host: str
    port: int
    token: str = field(repr=False)
    server_host: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "host",
            _normalize_ip_address(self.host, ERR_NOTIFICATION_HOST_INVALID, True),
        )

        if self.server_host is not None:
            object.__setattr__(
                self,
                "server_host",
                _normalize_ip_address(
                    self.server_host,
                    ERR_NOTIFICATION_SERVER_HOST_INVALID,
                    False,
                ),
            )

        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError(t(ERR_NOTIFICATION_PORT_INVALID))

    @property
    def connect_host(self) -> str:
        if self.server_host is not None:
            return self.server_host

        if self.host == "0.0.0.0":
            return "127.0.0.1"

        if self.host == "::":
            return "::1"

        return self.host


def load_notification_config() -> NotificationConfig:
    """Load listen/connect addresses and an explicit or derived authentication token.

    This factory performs disk I/O through dotenv and system secret loading;
    asynchronous callers should invoke it with ``asyncio.to_thread``.
    """
    load_dotenv()

    host_value = os.getenv("NOTIFICATION_HOST", NOTIFICATION_DEFAULT_HOST)
    if not host_value.strip():
        host = NOTIFICATION_DEFAULT_HOST
    else:
        host = host_value.strip()

    server_host_value = os.getenv("NOTIFICATION_SERVER_HOST")
    if server_host_value is not None:
        server_host_value = server_host_value.strip()
    server_host = server_host_value or None

    port_value = os.getenv("NOTIFICATION_PORT", str(NOTIFICATION_DEFAULT_PORT))
    if not port_value.strip():
        port_value = str(NOTIFICATION_DEFAULT_PORT)

    try:
        port = int(port_value.strip())
    except ValueError:
        raise ValueError(t(ERR_NOTIFICATION_PORT_INVALID)) from None

    if not 1 <= port <= 65535:
        raise ValueError(t(ERR_NOTIFICATION_PORT_INVALID))

    token_value = os.getenv("NOTIFICATION_TOKEN")
    token = ""
    if token_value is not None:
        token = token_value.strip().lower()

    if token and (len(token) != 64 or any(character not in "0123456789abcdef" for character in token)):
        raise ValueError(t(ERR_NOTIFICATION_TOKEN_INVALID))

    if not token:
        token = hmac.new(
            get_channel_encryption_key(),
            NOTIFICATION_AUTH_CONTEXT,
            hashlib.sha256,
        ).hexdigest()

    return NotificationConfig(
        host=host,
        port=port,
        token=token,
        server_host=server_host,
    )


__all__ = ["NotificationConfig", "load_notification_config"]
