import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError as ChromaNotFoundError

from app.core.constants import (
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
    ERR_VECTOR_ITEM_LENGTH_MISMATCH,
)
from app.core.i18n import t
from app.core.paths import CHROMA_DB_PATH, ensure_data_dirs

ensure_data_dirs()

chroma_client = chromadb.PersistentClient(
    path=str(CHROMA_DB_PATH),
    settings=Settings(anonymized_telemetry=False),
)


@dataclass(frozen=True, slots=True)
class CollectionValidationResult:
    exists: bool
    valid: bool
    count: int | None = None
    metadata: dict[str, Any] | None = None
    sample_dimension: int | None = None
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "valid": self.valid,
            "count": self.count,
            "metadata": self.metadata,
            "sample_dimension": self.sample_dimension,
            "errors": list(self.errors),
        }


def get_chroma_client():
    return chroma_client


def _collection_metadata(metadata: dict[str, Any] | None = None, distance: str | None = None) -> dict[str, Any] | None:
    if metadata is None and distance is None:
        return None
    normalized = dict(metadata or {})
    if distance is not None:
        normalized["hnsw:space"] = distance
    return normalized


def create_collection(collection_name: str, metadata: dict[str, Any] | None = None, distance: str | None = None):
    collection_metadata = _collection_metadata(metadata, distance)
    if collection_metadata is None:
        return chroma_client.create_collection(name=collection_name)
    return chroma_client.create_collection(name=collection_name, metadata=collection_metadata)


def get_or_create_collection(collection_name: str, metadata: dict[str, Any] | None = None, distance: str | None = None):
    collection_metadata = _collection_metadata(metadata, distance)
    if collection_metadata is None:
        return chroma_client.get_or_create_collection(name=collection_name)
    return chroma_client.get_or_create_collection(name=collection_name, metadata=collection_metadata)


def get_collection(collection_name: str):
    return chroma_client.get_collection(name=collection_name)


def delete_collection(collection_name: str):
    chroma_client.delete_collection(name=collection_name)


def delete_collection_items(collection_name: str, ids: list[str]):
    if not ids:
        return
    collection = get_collection(collection_name)
    collection.delete(ids=ids)


def get_collection_items(collection_name: str, include: list[str] | None = None):
    collection = get_collection(collection_name)
    return collection.get(include=include or ["documents", "metadatas"])


def _validate_item_lengths(item_ids: Sequence[str], documents: Sequence[str], embeddings: Sequence[Sequence[float]], metadatas: Sequence[dict[str, Any]]) -> int:
    lengths = {len(item_ids), len(documents), len(embeddings), len(metadatas)}
    if len(lengths) != 1:
        raise ValueError(t(ERR_VECTOR_ITEM_LENGTH_MISMATCH))
    return len(item_ids)


def _upsert_collection_items_sync(
    collection_name: str,
    item_ids: Sequence[str],
    documents: Sequence[str],
    embeddings: Sequence[Sequence[float]],
    metadatas: Sequence[dict[str, Any]],
    batch_size: int,
) -> int:
    item_count = _validate_item_lengths(item_ids, documents, embeddings, metadatas)
    if not item_count:
        return 0
    if batch_size <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="batch_size"))

    collection = get_collection(collection_name)
    for start in range(0, item_count, batch_size):
        end = start + batch_size
        collection.upsert(
            ids=list(item_ids[start:end]),
            documents=list(documents[start:end]),
            embeddings=[list(embedding) for embedding in embeddings[start:end]],
            metadatas=list(metadatas[start:end]),
        )
    return item_count


async def async_create_collection(collection_name: str, metadata: dict[str, Any] | None = None, distance: str | None = None):
    return await asyncio.to_thread(create_collection, collection_name, metadata, distance)


async def async_get_or_create_collection(collection_name: str, metadata: dict[str, Any] | None = None, distance: str | None = None):
    return await asyncio.to_thread(get_or_create_collection, collection_name, metadata, distance)


async def async_get_collection(collection_name: str):
    return await asyncio.to_thread(get_collection, collection_name)


async def async_delete_collection(collection_name: str) -> None:
    await asyncio.to_thread(delete_collection, collection_name)


async def async_upsert_collection_items(
    collection_name: str,
    item_ids: Sequence[str],
    documents: Sequence[str],
    embeddings: Sequence[Sequence[float]],
    metadatas: Sequence[dict[str, Any]],
    batch_size: int = 100,
) -> int:
    _validate_item_lengths(item_ids, documents, embeddings, metadatas)
    if batch_size <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="batch_size"))
    return await asyncio.to_thread(
        _upsert_collection_items_sync,
        collection_name,
        item_ids,
        documents,
        embeddings,
        metadatas,
        batch_size,
    )


def _get_collection_items_page_sync(collection_name: str, offset: int, limit: int | None, include: list[str] | None):
    if offset < 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="offset"))
    if limit is not None and limit <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="limit"))
    collection = get_collection(collection_name)
    kwargs: dict[str, Any] = {"offset": offset, "include": include or ["documents", "metadatas"]}
    if limit is not None:
        kwargs["limit"] = limit
    return collection.get(**kwargs)


async def async_get_collection_items(
    collection_name: str,
    offset: int = 0,
    limit: int | None = None,
    include: list[str] | None = None,
):
    return await asyncio.to_thread(_get_collection_items_page_sync, collection_name, offset, limit, include)


async def async_delete_collection_items(collection_name: str, ids: Sequence[str], batch_size: int = 100) -> int:
    if batch_size <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="batch_size"))
    item_ids = list(ids)
    if not item_ids:
        return 0

    def delete_items() -> int:
        collection = get_collection(collection_name)
        for start in range(0, len(item_ids), batch_size):
            collection.delete(ids=item_ids[start : start + batch_size])
        return len(item_ids)

    return await asyncio.to_thread(delete_items)


def _validate_collection_sync(
    collection_name: str,
    expected_count: int | None,
    expected_metadata: dict[str, Any] | None,
    expected_dimension: int | None,
    sample_size: int,
) -> CollectionValidationResult:
    if sample_size <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="sample_size"))
    try:
        collection = get_collection(collection_name)
    except ChromaNotFoundError:
        return CollectionValidationResult(exists=False, valid=False, errors=("collection_not_found",))

    count = collection.count()
    metadata = dict(collection.metadata or {})
    errors: list[str] = []
    if expected_count is not None and count != expected_count:
        errors.append("count_mismatch")
    if expected_metadata:
        for key, value in expected_metadata.items():
            if metadata.get(key) != value:
                errors.append(f"metadata_mismatch:{key}")

    sample_dimension = None
    if count:
        sample = collection.get(limit=min(sample_size, count), include=["embeddings"])
        raw_embeddings = sample.get("embeddings")
        vectors = [] if raw_embeddings is None else raw_embeddings
        dimensions: set[int] = set()
        for vector in vectors:
            if vector is None:
                continue
            try:
                dimensions.add(len(vector))
            except TypeError:
                errors.append("sample_dimension_invalid")
        if len(dimensions) > 1:
            errors.append("sample_dimension_inconsistent")
        if dimensions:
            sample_dimension = next(iter(dimensions))
            if expected_dimension is not None and sample_dimension != expected_dimension:
                errors.append("dimension_mismatch")
        elif expected_dimension is not None:
            errors.append("sample_dimension_missing")
    elif expected_dimension is not None:
        errors.append("sample_dimension_missing")

    return CollectionValidationResult(
        exists=True,
        valid=not errors,
        count=count,
        metadata=metadata,
        sample_dimension=sample_dimension,
        errors=tuple(errors),
    )


async def async_validate_collection(
    collection_name: str,
    expected_count: int | None = None,
    expected_metadata: dict[str, Any] | None = None,
    expected_dimension: int | None = None,
    sample_size: int = 1,
) -> CollectionValidationResult:
    return await asyncio.to_thread(
        _validate_collection_sync,
        collection_name,
        expected_count,
        expected_metadata,
        expected_dimension,
        sample_size,
    )


def _delete_orphan_items_sync(collection_name: str, valid_item_ids: set[str], batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="batch_size"))
    collection = get_collection(collection_name)
    total = collection.count()
    orphan_ids: list[str] = []
    for offset in range(0, total, batch_size):
        page = collection.get(offset=offset, limit=batch_size, include=["documents"])
        orphan_ids.extend(item_id for item_id in page.get("ids", []) if item_id not in valid_item_ids)

    for start in range(0, len(orphan_ids), batch_size):
        collection.delete(ids=orphan_ids[start : start + batch_size])
    return len(orphan_ids)


async def async_delete_orphan_items(collection_name: str, valid_item_ids: set[str], batch_size: int = 100) -> int:
    return await asyncio.to_thread(_delete_orphan_items_sync, collection_name, set(valid_item_ids), batch_size)


create_collection_async = async_create_collection
get_or_create_collection_async = async_get_or_create_collection
get_collection_async = async_get_collection
delete_collection_async = async_delete_collection
upsert_collection_items_async = async_upsert_collection_items
get_collection_items_async = async_get_collection_items
delete_collection_items_async = async_delete_collection_items
validate_collection_async = async_validate_collection
delete_orphan_items_async = async_delete_orphan_items
