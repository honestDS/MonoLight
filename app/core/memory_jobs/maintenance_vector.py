from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping, Sized
from numbers import Real
from typing import Any

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
            raise ValueError("collection items must be a dictionary")
        ids = raw.get("ids")
        documents = raw.get("documents")
        metadatas = raw.get("metadatas")
        embeddings = raw.get("embeddings")
        for values in (ids, documents, metadatas, embeddings):
            if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable) or not isinstance(values, Sized):
                raise ValueError("collection item fields must be sequences")
        ids = list(ids)
        documents = list(documents)
        metadatas = list(metadatas)
        embeddings = list(embeddings)
        if len({len(ids), len(documents), len(metadatas), len(embeddings)}) != 1:
            raise ValueError("collection item lengths must match")

        items: dict[str, tuple[str, dict[str, Any], list[float]]] = {}
        vector_dimension: int | None = None
        for index, item_id in enumerate(ids):
            if not isinstance(item_id, str) or not item_id or item_id in items:
                raise ValueError("collection item ID is invalid")
            document = documents[index]
            metadata = metadatas[index]
            vector = embeddings[index]
            if not isinstance(document, str) or not isinstance(metadata, dict):
                raise ValueError("collection item document or metadata is invalid")
            if isinstance(vector, (str, bytes, bytearray, Mapping)) or not isinstance(vector, Iterable) or not isinstance(vector, Sized):
                raise ValueError("collection item embedding is invalid")
            vector = list(vector)
            if not vector:
                raise ValueError("collection item embedding is invalid")
            if vector_dimension is None:
                vector_dimension = len(vector)
            elif len(vector) != vector_dimension:
                raise ValueError("collection item embedding dimensions must match")
            normalized_vector: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                    raise ValueError("collection item embedding value is invalid")
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
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc
    except Exception as exc:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc
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
        current_items = await collection_items(collection_name)

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
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED) from exc
    if not getattr(validation, "exists", False) or getattr(validation, "valid", True) is False:
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
    final_items = await collection_items(collection_name)
    if set(final_items) != set(expected_items):
        raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
    for item_id, expected in expected_items.items():
        actual = final_items.get(item_id)
        if actual is None or actual[:2] != expected:
            raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)

    for start in range(0, len(records), BATCH_SIZE):
        await context.checkpoint()
        batch_records = records[start : start + BATCH_SIZE]
        expected_vectors = await embed_records(context, batch_records, config)
        for record, expected_vector in zip(batch_records, expected_vectors):
            actual = final_items.get(record.vector_item_id)
            if actual is None:
                raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
            actual_vector = actual[2]
            if len(actual_vector) != len(expected_vector):
                raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)
            for actual_value, expected_value in zip(actual_vector, expected_vector):
                if actual_value == expected_value:
                    continue
                try:
                    float32_round_trip = struct.unpack("!f", struct.pack("!f", expected_value))[0]
                except OverflowError:
                    float32_round_trip = None
                if actual_value != float32_round_trip:
                    raise retryable(ERR_MEMORY_COLLECTION_VALIDATION_FAILED)

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
