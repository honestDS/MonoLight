from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.weixin_openclaw.constants import (
    DEFAULT_API_TIMEOUT_MS,
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    DEFAULT_CDN_BASE_URL,
    DEFAULT_CHANNEL_VERSION,
    DEFAULT_LONG_POLL_TIMEOUT_MS,
    DEFAULT_MAX_INBOUND_MEDIA_SIZE_MB,
    DEFAULT_MERGE_SINGLE_POLL_MESSAGES,
    DEFAULT_POLL_INTERVAL_MS,
)


def default_weixin_openclaw_config() -> dict[str, Any]:
    return {
        "bot_type": DEFAULT_BOT_TYPE,
        "channel_version": DEFAULT_CHANNEL_VERSION,
        "cdn_base_url": DEFAULT_CDN_BASE_URL,
        "api_timeout_ms": DEFAULT_API_TIMEOUT_MS,
        "long_poll_timeout_ms": DEFAULT_LONG_POLL_TIMEOUT_MS,
        "poll_interval_ms": DEFAULT_POLL_INTERVAL_MS,
        "max_inbound_media_size_mb": DEFAULT_MAX_INBOUND_MEDIA_SIZE_MB,
        "merge_single_poll_messages": DEFAULT_MERGE_SINGLE_POLL_MESSAGES,
    }


def normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_weixin_openclaw_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = {**default_weixin_openclaw_config(), **dict(config or {})}
    normalized["bot_type"] = DEFAULT_BOT_TYPE
    normalized["channel_version"] = DEFAULT_CHANNEL_VERSION
    normalized["cdn_base_url"] = str(normalized.get("cdn_base_url") or DEFAULT_CDN_BASE_URL).rstrip("/")
    api_timeout_ms = normalized.get("api_timeout_ms")
    long_poll_timeout_ms = normalized.get("long_poll_timeout_ms")
    poll_interval_ms = normalized.get("poll_interval_ms")
    max_inbound_media_size_mb = normalized.get("max_inbound_media_size_mb")
    normalized["api_timeout_ms"] = max(1_000, int(DEFAULT_API_TIMEOUT_MS if api_timeout_ms in (None, "") else api_timeout_ms))
    normalized["long_poll_timeout_ms"] = max(1_000, int(DEFAULT_LONG_POLL_TIMEOUT_MS if long_poll_timeout_ms in (None, "") else long_poll_timeout_ms))
    normalized["poll_interval_ms"] = max(0, int(DEFAULT_POLL_INTERVAL_MS if poll_interval_ms in (None, "") else poll_interval_ms))
    normalized["max_inbound_media_size_mb"] = max(1, int(DEFAULT_MAX_INBOUND_MEDIA_SIZE_MB if max_inbound_media_size_mb in (None, "") else max_inbound_media_size_mb))
    normalized["merge_single_poll_messages"] = normalize_bool(normalized.get("merge_single_poll_messages"), DEFAULT_MERGE_SINGLE_POLL_MESSAGES)
    return normalized


@dataclass
class WeixinOpenClawConfig:
    token: str = ""
    base_url: str = DEFAULT_BASE_URL
    cdn_base_url: str = DEFAULT_CDN_BASE_URL
    bot_type: str = DEFAULT_BOT_TYPE
    sync_buf: str = ""
    account_id: str = ""
    channel_version: str = DEFAULT_CHANNEL_VERSION
    api_timeout_ms: int = DEFAULT_API_TIMEOUT_MS
    long_poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS
    max_inbound_media_size_mb: int = DEFAULT_MAX_INBOUND_MEDIA_SIZE_MB
    merge_single_poll_messages: bool = DEFAULT_MERGE_SINGLE_POLL_MESSAGES
