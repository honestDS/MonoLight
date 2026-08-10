from __future__ import annotations

import hashlib
import re
from typing import Any

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_MEMORY_COLLECTION_PURPOSE_INVALID,
    ERR_MEMORY_EMBEDDING_SIGNATURE_REQUIRED,
    ERR_MEMORY_MUTATION_TARGET_INVALID,
    ERR_MEMORY_UID_REQUIRED,
    ERR_VALUE_MUST_BE_BETWEEN,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t
from app.core.memory.errors import MemoryValidationError
from app.core.memory.normalization import _normalize_uid, _require_positive, normalize_memory_key

_PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _validate_positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field=name))


def build_memory_collection_name(uid: str, embedding_signature: str, revision: int, purpose: str) -> str:
    if not isinstance(uid, str) or not uid:
        raise ValueError(t(ERR_MEMORY_UID_REQUIRED, field="uid"))
    if not isinstance(embedding_signature, str) or not embedding_signature:
        raise ValueError(t(ERR_MEMORY_EMBEDDING_SIGNATURE_REQUIRED, field="embedding_signature"))
    if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= 9_999_999_999:
        raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="revision", minimum=0, maximum=9_999_999_999))
    if not isinstance(purpose, str) or not 1 <= len(purpose) <= 16 or _PURPOSE_PATTERN.fullmatch(purpose) is None:
        raise ValueError(t(ERR_MEMORY_COLLECTION_PURPOSE_INVALID, field="purpose"))

    uid_digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    signature_digest = hashlib.sha256(embedding_signature.encode("utf-8")).hexdigest()[:10]
    return f"memory_{purpose}_{uid_digest}_{signature_digest}_{revision}"


def build_memory_vector_item_id(memory_id: int, version: int) -> str:
    _validate_positive_integer(memory_id, "memory_id")
    _validate_positive_integer(version, "version")
    return f"memory_{memory_id}_v{version}"


def build_memory_active_mutation_key(
    uid: str,
    memory_id: int | None = None,
    memory_key: str | None = None,
) -> str:
    normalized_uid = _normalize_uid(uid)
    if (memory_id is None) == (memory_key is None):
        raise MemoryValidationError(ERR_MEMORY_MUTATION_TARGET_INVALID)
    if memory_id is not None:
        target: dict[str, Any] = {"memory_id": _require_positive(memory_id, field="memory_id")}
    else:
        target = {"memory_key": normalize_memory_key(memory_key or "")}
    canonical = canonical_json_dumps({"uid": normalized_uid, **target})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_memory_organization_active_mutation_key(uid: str) -> str:
    normalized_uid = _normalize_uid(uid)
    canonical = canonical_json_dumps({"scope": "memory_organization", "uid": normalized_uid})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "build_memory_active_mutation_key",
    "build_memory_collection_name",
    "build_memory_organization_active_mutation_key",
    "build_memory_vector_item_id",
]
