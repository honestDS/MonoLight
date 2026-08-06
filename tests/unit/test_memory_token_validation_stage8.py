from __future__ import annotations

import hashlib

import pytest

from app.core.constants import ERR_MEMORY_JOB_PAYLOAD_INVALID, MEMORY_CONTENT_MAX_TOKENS
from app.core.memory import (
    MemoryContentTooLongError,
    MemoryValidationError,
    normalize_memory_content,
    normalize_memory_content_for_publication,
    normalize_memory_publication_payload,
    normalize_memory_record_snapshot,
)
from app.core.utils.tokenizer import estimate_tokens


def _publication_payload(content: str) -> dict[str, object]:
    normalized = normalize_memory_content_for_publication(content)
    return {
        "memory_key": "profile.fact",
        "content": normalized.content,
        "content_hash": normalized.content_hash,
        "memory_type": "fact",
        "source": "user_api",
        "source_id": None,
        "source_session_id": None,
        "source_profile_id": None,
        "source_message_id": None,
        "change_evidence": None,
        "content_token_count": normalized.content_token_count,
    }


def _record_snapshot(content: str, *, include_token_count: bool = True) -> dict[str, object]:
    normalized_content = normalize_memory_content(content)
    normalized_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    normalized_token_count = estimate_tokens(normalized_content)
    snapshot: dict[str, object] = {
        "memory_key": "profile.fact",
        "content": normalized_content,
        "content_hash": normalized_hash,
        "memory_type": "fact",
        "source": "user_api",
        "source_id": None,
        "source_session_id": None,
        "source_profile_id": None,
        "source_message_id": None,
        "source_job_id": None,
        "change_evidence": None,
        "version": 1,
    }
    if include_token_count:
        snapshot["content_token_count"] = normalized_token_count
    return snapshot


def test_publication_content_result_normalizes_hashes_and_counts_tokens() -> None:
    result = normalize_memory_content_for_publication("  Ａ\tB\nＣ  ")

    assert result.content == "A B C"
    assert result.normalized_content == "A B C"
    assert result.content_hash == hashlib.sha256(b"A B C").hexdigest()
    assert result.content_token_count >= 0


def test_publication_content_result_rejects_over_limit_without_truncation() -> None:
    content = " ".join(["oversized"] * (MEMORY_CONTENT_MAX_TOKENS + 20))

    with pytest.raises(MemoryContentTooLongError) as exc_info:
        normalize_memory_content_for_publication(content)

    assert exc_info.value.data == {
        "status": "content_too_long",
        "actual_tokens": exc_info.value.data["actual_tokens"],
        "max_tokens": MEMORY_CONTENT_MAX_TOKENS,
        "retryable": True,
    }
    assert exc_info.value.data["actual_tokens"] > MEMORY_CONTENT_MAX_TOKENS
    assert len(content) > 160


def test_publication_payload_fills_legacy_token_count_and_rejects_inconsistent_values() -> None:
    payload = _publication_payload("short memory")
    expected_count = payload["content_token_count"]
    payload.pop("content_token_count")

    normalized = normalize_memory_publication_payload(payload)

    assert normalized["content_token_count"] == expected_count
    for invalid_value in (True, -1, expected_count + 1):
        invalid_payload = dict(normalized)
        invalid_payload["content_token_count"] = invalid_value
        with pytest.raises(MemoryValidationError) as exc_info:
            normalize_memory_publication_payload(invalid_payload)
        assert exc_info.value.message == ERR_MEMORY_JOB_PAYLOAD_INVALID


def test_record_snapshot_persists_token_count_and_fills_legacy_snapshot_without_limit() -> None:
    short_snapshot = _record_snapshot("short memory")
    normalized_short = normalize_memory_record_snapshot(short_snapshot)
    assert normalized_short["content_token_count"] == short_snapshot["content_token_count"]

    legacy_snapshot = _record_snapshot("legacy memory", include_token_count=False)
    normalized_legacy = normalize_memory_record_snapshot(legacy_snapshot)
    assert normalized_legacy["content_token_count"] == normalize_memory_content_for_publication("legacy memory").content_token_count

    oversized_content = " ".join(["legacy"] * (MEMORY_CONTENT_MAX_TOKENS + 20))
    oversized_snapshot = _record_snapshot(oversized_content, include_token_count=False)
    normalized_oversized = normalize_memory_record_snapshot(oversized_snapshot)
    assert normalized_oversized["content_token_count"] > MEMORY_CONTENT_MAX_TOKENS

    invalid_snapshot = dict(normalized_short)
    invalid_snapshot["content_token_count"] = normalized_short["content_token_count"] + 1
    with pytest.raises(MemoryValidationError) as exc_info:
        normalize_memory_record_snapshot(invalid_snapshot)
    assert exc_info.value.message == ERR_MEMORY_JOB_PAYLOAD_INVALID
