from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterable, Mapping, Sized
from numbers import Real
from typing import Any, NoReturn

from app.core.constants import (
    ERR_MEMORY_COLLECTION_VALIDATION_FAILED,
    ERR_MEMORY_EMBEDDING_VECTOR_INVALID,
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_EMBEDDING_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID,
    ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
)
from app.core.embedding.common import embed_texts_with_config
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
)
from app.core.memory_jobs.maintenance_state import (
    BATCH_SIZE,
    RecordSnapshot,
    ValidationSnapshot,
    collection_metadata,
    deterministic,
    load_runtime,
    record_metadata,
    record_snapshot,
    retryable,
)
from app.core.retrieval.hybrid import hybrid_query_collection
from app.models.memory import LongTermMemoryRecord
from app.providers.vector import (
    async_delete_collection,
    async_delete_orphan_items,
    async_get_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
    async_validate_collection,
)

logger = get_logger(__name__)

_VECTOR_COMPARISON_REL_TOL = 1e-5
_VECTOR_COMPARISON_ABS_TOL = 1e-6


def _vector_digest(vector: list[float]) -> str:
    hasher = hashlib.sha256()
    for value in vector:
        hasher.update(struct.pack("!d", value))
    return hasher.hexdigest()


def _vector_norm(vector: list[float]) -> float:
    return math.hypot(*vector)


def _cosine_similarity(
    actual_vector: list[float],
    expected_vector: list[float],
    actual_norm: float,
    expected_norm: float,
) -> float | None:
    if actual_norm == 0.0 or expected_norm == 0.0:
        return None
    return math.fsum((actual_value / actual_norm) * (expected_value / expected_norm) for actual_value, expected_value in zip(actual_vector, expected_vector))


def _batch_vector_diagnostics(
    records: list[LongTermMemoryRecord],
    vectors: list[list[float]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for record, vector in zip(records, vectors):
        diagnostics.append(
            {
                "memory_id": record.id,
                "vector_item_id": record.vector_item_id,
                "vector_digest": _vector_digest(vector),
                "vector_norm": _vector_norm(vector),
                "dimension": len(vector),
            }
        )
    return diagnostics


def _raise_collection_validation_failure(
    context: MemoryJobExecutionContext,
    collection_name: str,
    *,
    stage: str,
    category: str,
    difference_summary: str,
    cause: BaseException | None = None,
) -> NoReturn:
    logger.bind(
        uid=context.job.uid,
        job_id=context.job.id,
        collection_name=collection_name,
        validation_stage=stage,
        failure_category=category,
        difference_summary=difference_summary,
    ).warning(t(ERR_MEMORY_COLLECTION_VALIDATION_FAILED))
    error = retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
    if cause is None:
        raise error
    raise error from cause


async def embed_records(
    context: MemoryJobExecutionContext,
    records: list[LongTermMemoryRecord],
    config: dict[str, Any],
) -> list[list[float]]:
    runtime = await load_runtime(context, config["channel_id"], config["model_id"])
    if runtime.channel_id != config["channel_id"] or runtime.model_id != config["model_id"]:
        raise retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
    if runtime.declared_dimensions is not None and runtime.declared_dimensions != config["dimensions"]:
        raise retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
    contents: list[str] = []
    for record in records:
        contents.append(record.content)
    try:
        embeddings = await embed_texts_with_config(
            runtime,
            contents,
            batch_size=BATCH_SIZE,
            dimensions=config["dimensions"],
        )
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        raise retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
    if not isinstance(embeddings, list) or len(embeddings) != len(records):
        raise deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    validated: list[list[float]] = []
    for vector in embeddings:
        if not isinstance(vector, list) or len(vector) != config["dimensions"]:
            raise deterministic(ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID)
        for item in vector:
            if isinstance(item, bool) or not isinstance(item, Real) or not math.isfinite(float(item)):
                raise deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
        normalized_vector: list[float] = []
        for item in vector:
            normalized_vector.append(float(item))
        validated.append(normalized_vector)
    return validated


async def upsert_records(
    context: MemoryJobExecutionContext,
    collection_name: str,
    records: list[LongTermMemoryRecord],
    config: dict[str, Any],
) -> None:
    if not records:
        return
    vectors = await embed_records(context, records, config)
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record.vector_item_id, str) or not record.vector_item_id:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        ids.append(record.vector_item_id)
        documents.append(record.content)
        metadatas.append(record_metadata(record, config["embedding_revision"]))
    try:
        await async_upsert_collection_items(
            collection_name,
            ids,
            documents,
            vectors,
            metadatas,
            batch_size=BATCH_SIZE,
        )
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc
    logger.bind(
        uid=context.job.uid,
        job_id=context.job.id,
        collection_name=collection_name,
        vector_stage="upsert_completed",
        vector_diagnostics=_batch_vector_diagnostics(records, vectors),
    ).debug("Memory vector batch upsert diagnostics")


async def ensure_collection(
    *,
    uid: str,
    config: dict[str, Any],
    purpose: str,
    reset: bool,
) -> None:
    collection_name = config["collection"]
    try:
        validation = await async_validate_collection(collection_name)
        exists = bool(getattr(validation, "exists", False))
    except Exception as exc:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc
    if reset and exists:
        try:
            await async_delete_collection(collection_name)
        except Exception as exc:
            raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc
    if reset or not exists:
        try:
            await async_get_or_create_collection(
                collection_name,
                metadata=collection_metadata(uid, config, purpose),
                distance="cosine",
            )
        except Exception as exc:
            raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc


async def collection_items(
    collection_name: str,
) -> dict[str, tuple[str, dict[str, Any], list[float]]]:
    try:
        raw = await async_get_collection_items(
            collection_name,
            include=["documents", "metadatas", "embeddings"],
        )
        if not isinstance(raw, dict):
            raise ValueError
        ids = raw.get("ids")
        documents = raw.get("documents")
        metadatas = raw.get("metadatas")
        embeddings = raw.get("embeddings")
        for values in (ids, documents, metadatas, embeddings):
            if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable) or not isinstance(values, Sized):
                raise ValueError
        ids = list(ids)
        documents = list(documents)
        metadatas = list(metadatas)
        embeddings = list(embeddings)
        if len({len(ids), len(documents), len(metadatas), len(embeddings)}) != 1:
            raise ValueError

        items: dict[str, tuple[str, dict[str, Any], list[float]]] = {}
        vector_dimension: int | None = None
        for index, item_id in enumerate(ids):
            if not isinstance(item_id, str) or not item_id or item_id in items:
                raise ValueError
            document = documents[index]
            metadata = metadatas[index]
            vector = embeddings[index]
            if not isinstance(document, str) or not isinstance(metadata, dict):
                raise ValueError
            if isinstance(vector, (str, bytes, bytearray, Mapping)) or not isinstance(vector, Iterable) or not isinstance(vector, Sized):
                raise ValueError
            vector = list(vector)
            if not vector:
                raise ValueError
            if vector_dimension is None:
                vector_dimension = len(vector)
            elif len(vector) != vector_dimension:
                raise ValueError
            normalized_vector: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                    raise ValueError
                normalized_vector.append(float(value))
            items[item_id] = (document, dict(metadata), normalized_vector)
        return items
    except Exception as exc:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc


async def reconcile_collection(
    context: MemoryJobExecutionContext,
    *,
    records: list[LongTermMemoryRecord],
    config: dict[str, Any],
    purpose: str,
) -> ValidationSnapshot:
    collection_name = config["collection"]
    expected_collection_metadata = collection_metadata(context.job.uid, config, purpose)
    try:
        validation = await async_validate_collection(
            collection_name,
            expected_metadata=expected_collection_metadata,
            expected_dimension=None,
        )
    except TypeError:
        try:
            validation = await async_validate_collection(collection_name)
        except Exception as exc:
            _raise_collection_validation_failure(
                context,
                collection_name,
                stage="initial_collection_validation_fallback",
                category="validation_request_failed",
                difference_summary="collection_validation_request_failed",
                cause=exc,
            )
    except Exception as exc:
        _raise_collection_validation_failure(
            context,
            collection_name,
            stage="initial_collection_validation",
            category="validation_request_failed",
            difference_summary="collection_validation_request_failed",
            cause=exc,
        )
    exists = bool(getattr(validation, "exists", False))
    actual_metadata = getattr(validation, "metadata", None)
    metadata_matches = isinstance(actual_metadata, dict)
    if metadata_matches:
        for key, value in expected_collection_metadata.items():
            if actual_metadata.get(key) != value:
                metadata_matches = False
                break
    if not exists or not metadata_matches:
        if exists:
            try:
                await async_delete_collection(collection_name)
            except Exception as exc:
                raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc
        try:
            await async_get_or_create_collection(
                collection_name,
                metadata=expected_collection_metadata,
                distance="cosine",
            )
        except Exception as exc:
            raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc
        current_items: dict[str, tuple[str, dict[str, Any], list[float]]] = {}
    else:
        try:
            current_items = await collection_items(collection_name)
        except MemoryJobExecutionError:
            _raise_collection_validation_failure(
                context,
                collection_name,
                stage="current_collection_items",
                category="collection_items_invalid",
                difference_summary="current_collection_items_validation_failed",
            )

    expected_items: dict[str, tuple[str, dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record.vector_item_id, str) or not record.vector_item_id:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        expected_items[record.vector_item_id] = (
            record.content,
            record_metadata(record, config["embedding_revision"]),
        )

    mismatched: list[LongTermMemoryRecord] = []
    for record in records:
        expected = expected_items.get(record.vector_item_id)
        current = current_items.get(record.vector_item_id)
        if current is None or current[:2] != expected:
            mismatched.append(record)
    for start in range(0, len(mismatched), BATCH_SIZE):
        await context.checkpoint()
        await upsert_records(context, collection_name, mismatched[start : start + BATCH_SIZE], config)
    try:
        await async_delete_orphan_items(collection_name, set(expected_items), batch_size=BATCH_SIZE)
    except Exception as exc:
        raise retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

    try:
        validation = await async_validate_collection(
            collection_name,
            expected_count=len(records),
            expected_metadata=expected_collection_metadata,
            expected_dimension=config["dimensions"] if records else None,
        )
    except Exception as exc:
        _raise_collection_validation_failure(
            context,
            collection_name,
            stage="final_collection_validation",
            category="validation_request_failed",
            difference_summary="collection_validation_request_failed",
            cause=exc,
        )
    if not getattr(validation, "exists", False) or getattr(validation, "valid", True) is False:
        _raise_collection_validation_failure(
            context,
            collection_name,
            stage="final_collection_validation",
            category="collection_validation_result_invalid",
            difference_summary="collection_missing_or_invalid",
        )
    try:
        final_items = await collection_items(collection_name)
    except MemoryJobExecutionError:
        _raise_collection_validation_failure(
            context,
            collection_name,
            stage="final_collection_items",
            category="collection_items_invalid",
            difference_summary="final_collection_items_validation_failed",
        )
    if set(final_items) != set(expected_items):
        _raise_collection_validation_failure(
            context,
            collection_name,
            stage="final_item_set_validation",
            category="item_set_mismatch",
            difference_summary=f"expected_count={len(expected_items)},actual_count={len(final_items)}",
        )
    for item_id, expected in expected_items.items():
        actual = final_items.get(item_id)
        if actual is None or actual[:2] != expected:
            _raise_collection_validation_failure(
                context,
                collection_name,
                stage="final_item_metadata_validation",
                category="item_payload_mismatch",
                difference_summary="item_document_or_metadata_mismatch",
            )

    for start in range(0, len(records), BATCH_SIZE):
        await context.checkpoint()
        batch_records = records[start : start + BATCH_SIZE]
        expected_vectors = await embed_records(context, batch_records, config)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            collection_name=collection_name,
            vector_stage="final_validation_regenerated",
            vector_diagnostics=_batch_vector_diagnostics(batch_records, expected_vectors),
        ).debug("Memory vector batch final validation diagnostics")
        for record, expected_vector in zip(batch_records, expected_vectors):
            actual = final_items.get(record.vector_item_id)
            if actual is None:
                _raise_collection_validation_failure(
                    context,
                    collection_name,
                    stage="final_vector_validation",
                    category="vector_item_missing",
                    difference_summary="expected_vector_item_missing",
                )
            actual_vector = actual[2]
            if len(actual_vector) != len(expected_vector):
                _raise_collection_validation_failure(
                    context,
                    collection_name,
                    stage="final_vector_validation",
                    category="vector_dimension_mismatch",
                    difference_summary=f"expected_dimension={len(expected_vector)},actual_dimension={len(actual_vector)}",
                )
            for coordinate, (actual_value, expected_value) in enumerate(zip(actual_vector, expected_vector)):
                if not math.isclose(
                    actual_value,
                    expected_value,
                    rel_tol=_VECTOR_COMPARISON_REL_TOL,
                    abs_tol=_VECTOR_COMPARISON_ABS_TOL,
                ):
                    mismatched_coordinate_count = 0
                    max_abs_difference = 0.0
                    for compared_actual_value, compared_expected_value in zip(actual_vector, expected_vector):
                        compared_absolute_difference = abs(compared_actual_value - compared_expected_value)
                        max_abs_difference = max(max_abs_difference, compared_absolute_difference)
                        if not math.isclose(
                            compared_actual_value,
                            compared_expected_value,
                            rel_tol=_VECTOR_COMPARISON_REL_TOL,
                            abs_tol=_VECTOR_COMPARISON_ABS_TOL,
                        ):
                            mismatched_coordinate_count += 1
                    actual_vector_norm = _vector_norm(actual_vector)
                    expected_vector_norm = _vector_norm(expected_vector)
                    absolute_difference = abs(actual_value - expected_value)
                    logger.bind(
                        uid=context.job.uid,
                        job_id=context.job.id,
                        collection_name=collection_name,
                        memory_id=record.id,
                        vector_item_id=record.vector_item_id,
                        vector_stage="final_validation_mismatch",
                        actual_vector_digest=_vector_digest(actual_vector),
                        expected_vector_digest=_vector_digest(expected_vector),
                        actual_vector_norm=actual_vector_norm,
                        expected_vector_norm=expected_vector_norm,
                        cosine_similarity=_cosine_similarity(
                            actual_vector,
                            expected_vector,
                            actual_vector_norm,
                            expected_vector_norm,
                        ),
                        mismatched_coordinate_count=mismatched_coordinate_count,
                        max_abs_difference=max_abs_difference,
                        first_mismatch_coordinate=coordinate,
                        actual_value=actual_value,
                        expected_value=expected_value,
                        absolute_difference=absolute_difference,
                    ).debug("Memory vector final validation mismatch diagnostics")
                    _raise_collection_validation_failure(
                        context,
                        collection_name,
                        stage="final_vector_validation",
                        category="vector_value_mismatch",
                        difference_summary=f"memory_id={record.id},coordinate={coordinate}",
                    )

    logger.bind(
        uid=context.job.uid,
        job_id=context.job.id,
        collection_name=collection_name,
        purpose=purpose,
        expected_count=len(records),
        expected_dimension=config["dimensions"] if records else None,
        repaired_count=len(mismatched),
    ).info("Memory vector collection reconciliation completed")

    snapshots: list[RecordSnapshot] = []
    for record in records:
        snapshots.append(record_snapshot(record, config["embedding_revision"]))
    return ValidationSnapshot(
        records=tuple(snapshots),
        count=len(records),
        success_count=len(records),
    )


async def validate_sample_query(
    context: MemoryJobExecutionContext,
    *,
    records: list[LongTermMemoryRecord],
    snapshot: ValidationSnapshot,
    config: dict[str, Any],
) -> None:
    if not records:
        return
    query_vectors = await embed_records(context, [records[0]], config)
    try:
        hits = await hybrid_query_collection(
            config["collection"],
            query_vectors[0],
            records[0].content,
            limit=min(5, len(records)),
        )
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc
    if not hits:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)

    valid_ids: set[str] = set()
    valid_memory_ids: set[int] = set()
    expected_metadata_by_id: dict[str, dict[str, Any]] = {}
    for item in snapshot.records:
        valid_ids.add(item.vector_item_id)
        valid_memory_ids.add(item.memory_id)
        expected_metadata_by_id[item.vector_item_id] = item.metadata

    sample_id = records[0].vector_item_id
    sample_hit = False
    for hit in hits:
        hit_id = getattr(hit, "id", None)
        metadata = getattr(hit, "metadata", None)
        if hit_id == sample_id:
            sample_hit = True
        if hit_id not in valid_ids:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        if not isinstance(metadata, dict):
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        if metadata.get("uid") != context.job.uid:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        if metadata.get("embedding_revision") != config["embedding_revision"]:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        if metadata.get("memory_id") not in valid_memory_ids:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        expected_metadata = expected_metadata_by_id.get(hit_id)
        if expected_metadata is None:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
        for key, value in expected_metadata.items():
            if metadata.get(key) != value:
                raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
    if not sample_hit:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)


__all__ = [
    "collection_items",
    "embed_records",
    "ensure_collection",
    "reconcile_collection",
    "upsert_records",
    "validate_sample_query",
]
