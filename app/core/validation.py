import re

from pydantic_core import PydanticCustomError

from app.core.constants import (
    ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS,
    ERR_CHANNEL_BASE_URL_SCHEME,
    ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID,
    ERR_CHANNEL_MODEL_PROTOCOL_REQUIRED,
    ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_VALIDATION_FAILED,
)
from app.core.i18n import t
from app.models.channel import MODEL_PROTOCOLS_BY_USAGE, ModelProtocol, ModelUsage

_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def validate_password(password: str, *, require_non_empty: bool = True, minimum_length: int = 8) -> str:
    if not isinstance(password, str):
        raise ValueError(t(ERR_VALIDATION_FAILED))
    if not password:
        if require_non_empty:
            raise ValueError(t("missing"))
        return password
    if len(password) < minimum_length:
        raise ValueError(t("string_too_short"))
    if len(password.encode("utf-8")) > 72:
        raise PydanticCustomError(ERR_PASSWORD_TOO_LONG_BYTES, t(ERR_PASSWORD_TOO_LONG_BYTES))
    return password


def validate_username(username: str) -> str:
    if not isinstance(username, str) or not username:
        raise ValueError(t("missing"))
    if len(username) < 3:
        raise ValueError(t("string_too_short"))
    if len(username) > 50:
        raise ValueError(t("string_too_long"))
    if _USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError(t(ERR_VALIDATION_FAILED))
    return username


def validate_base_url(base_url: str | None, *, model_ids: list[dict] | None = None) -> str | None:
    if not isinstance(base_url, (str, type(None))):
        raise ValueError(t(ERR_VALIDATION_FAILED))
    if model_ids and not base_url:
        raise ValueError(t(ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS))
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError(t(ERR_CHANNEL_BASE_URL_SCHEME))
    return base_url


def validate_chat_model(model_id: str, protocol: ModelProtocol | str) -> tuple[str, ModelProtocol]:
    normalized_model_id = model_id.strip() if isinstance(model_id, str) else ""
    if not normalized_model_id:
        raise ValueError(t(ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID))
    if not protocol:
        raise ValueError(t(ERR_CHANNEL_MODEL_PROTOCOL_REQUIRED))

    try:
        protocol_enum = protocol if isinstance(protocol, ModelProtocol) else ModelProtocol(protocol)
    except (TypeError, ValueError) as exc:
        raise ValueError(t(ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID)) from exc

    if protocol_enum not in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]:
        raise ValueError(t(ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID))
    return normalized_model_id, protocol_enum
