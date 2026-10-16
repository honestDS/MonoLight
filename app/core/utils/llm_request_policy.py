import json
from types import MappingProxyType
from typing import Any

from app.core.constants import ERR_LLM_REQUEST_TOO_LARGE, ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE
from app.core.crud.system.setting import system_setting_crud
from app.core.exceptions import LLMRequestRejectedException
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal

_MIB = 1024 * 1024
_UPSTREAM_REJECTION_RULES = MappingProxyType({413: (ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE, "request_too_large")})


async def load_llm_request_max_bytes() -> int:
    async with AsyncSessionLocal() as db:
        max_size_mb = await system_setting_crud.get_llm_request_max_size_mb(db)
    return max_size_mb * _MIB


def validate_llm_request_size(request_size_bytes: int, *, max_size_bytes: int) -> None:
    if request_size_bytes > max_size_bytes:
        raise LLMRequestRejectedException(
            message=ERR_LLM_REQUEST_TOO_LARGE,
            source="local",
            reason="request_too_large",
            allow_channel_fallback=False,
            code=400,
            data={"request_size_bytes": request_size_bytes, "max_size_bytes": max_size_bytes},
            request_size_bytes=request_size_bytes,
            max_size_bytes=max_size_bytes,
            request_size_mib=f"{request_size_bytes / _MIB:.2f}",
            max_size_mib=f"{max_size_bytes / _MIB:.2f}",
        )


def serialize_llm_json_request(payload: dict[str, Any], *, max_size_bytes: int) -> bytes:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = serialized.encode("utf-8")
    request_size_bytes = len(encoded)
    validate_llm_request_size(request_size_bytes, max_size_bytes=max_size_bytes)
    return encoded


async def prepare_llm_json_request(payload: dict[str, Any]) -> bytes:
    max_size_bytes = await load_llm_request_max_bytes()
    return serialize_llm_json_request(payload, max_size_bytes=max_size_bytes)


def build_upstream_rejection(*, status: int | None, detail: Any) -> LLMRequestRejectedException | None:
    rule = _UPSTREAM_REJECTION_RULES.get(status)
    if rule is None:
        return None

    message, reason = rule
    get_logger(__name__).bind(upstream_status=status, upstream_detail=detail).warning(
        "Upstream LLM request rejected: status={status}, response body={detail}",
        status=status,
        detail=detail,
    )
    return LLMRequestRejectedException(
        message=message,
        source="upstream",
        reason=reason,
        allow_channel_fallback=True,
        upstream_status=status,
        upstream_detail=detail,
        code=502,
    )
