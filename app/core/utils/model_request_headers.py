"""Model-specific HTTP request header helpers."""

import re

from app.core.constants import ERR_CHANNEL_MODEL_CUSTOM_HEADERS_INVALID
from app.core.i18n import t

_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RESERVED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "content-type",
        "content-length",
        "host",
        "connection",
        "transfer-encoding",
        "proxy-authorization",
        "proxy-connection",
    }
)


def _invalid_model_custom_headers() -> ValueError:
    return ValueError(t(ERR_CHANNEL_MODEL_CUSTOM_HEADERS_INVALID))


def _normalize_header_value(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise _invalid_model_custom_headers()
    if any(not 32 <= ord(character) <= 126 for character in value):
        raise _invalid_model_custom_headers()

    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise _invalid_model_custom_headers()
    return normalized


def normalize_legacy_model_user_agent(value: object) -> str:
    """Validate the persisted legacy user_agent value before migration."""
    return _normalize_header_value(value, max_length=512)


def normalize_model_custom_headers(value: object) -> dict[str, str]:
    """Validate and canonicalize model-specific HTTP request headers."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise _invalid_model_custom_headers()

    normalized_headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not _HEADER_NAME_PATTERN.fullmatch(raw_name):
            raise _invalid_model_custom_headers()
        name = raw_name.lower()
        if name in _RESERVED_HEADER_NAMES or name in normalized_headers:
            raise _invalid_model_custom_headers()
        normalized_headers[name] = _normalize_header_value(raw_value, max_length=4096)
    return normalized_headers


def get_model_custom_headers(model_entry: dict) -> dict[str, str]:
    """Read model request headers from persisted data without raising on dirty values."""
    if not isinstance(model_entry, dict):
        return {}
    advanced_settings = model_entry.get("advanced_settings")
    if not isinstance(advanced_settings, dict):
        return {}

    try:
        headers = normalize_model_custom_headers(advanced_settings.get("custom_headers"))
    except ValueError:
        headers = {}

    if "user-agent" not in headers and advanced_settings.get("user_agent") is not None:
        try:
            headers["user-agent"] = normalize_legacy_model_user_agent(advanced_settings["user_agent"])
        except ValueError:
            pass
    return headers


def build_model_request_headers(
    api_key: str,
    custom_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build model request headers without allowing reserved-header overrides."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(normalize_model_custom_headers(custom_headers))
    return headers
