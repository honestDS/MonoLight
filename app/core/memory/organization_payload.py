from __future__ import annotations

import math
from numbers import Real
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
)
from app.core.memory.errors import MemoryValidationError

__all__ = []

_ORGANIZATION_PAYLOAD_FIELDS = frozenset({"trigger", "snapshot", "organization_model"})

_ORGANIZATION_PLAN_CHECKPOINT_FIELDS = frozenset({"model_output", "usage", "finish_reason"})

_ORGANIZATION_TRIGGERS = frozenset({"manual", "auto"})

_ORGANIZATION_SNAPSHOT_FIELDS = frozenset(
    {
        "digest",
        "count",
        "active_embedding_revision",
        "index_revision",
        "policy_version",
        "items",
    }
)

_ORGANIZATION_MODEL_FIELDS = frozenset(
    {
        "channel_id",
        "channel_name",
        "model_id",
        "usage",
        "protocol",
        "base_url",
        "api_key",
        "http_proxy",
        "custom_headers",
        "temperature",
        "top_p",
        "timeout",
        "context_window_k",
        "context_window_tokens",
        "max_tokens",
        "snapshot_count",
        "required_output_tokens",
        "policy_version",
    }
)


def _raise_organization_payload_invalid() -> None:
    raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)


def _strict_non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _strict_text(value: Any, *, allow_blank: bool = False) -> bool:
    return isinstance(value, str) and (allow_blank or bool(value.strip()))


def _strict_number(value: Any, *, minimum: float, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum or (maximum is not None and normalized > maximum):
        return None
    return normalized
