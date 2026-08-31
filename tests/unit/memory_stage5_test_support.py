from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.models.profile import Profile
from app.providers.database.time import get_database_time

MEMORY_TABLES = (
    Profile.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
)


class Stage5VectorBackend:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.runtime_configs: dict[tuple[int, str], EmbeddingRuntimeConfig] = {}
        self.embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.deleted_collections: list[str] = []
        self.deleted_items: list[tuple[str, list[str]]] = []

    async def load_config(
        self,
        _db: Any,
        channel_id: int,
        model_id: str,
    ) -> EmbeddingRuntimeConfig:
        return self.runtime_configs[(channel_id, model_id)]

    async def embed(
        self,
        config: EmbeddingRuntimeConfig,
        texts: Sequence[str],
        dimensions: int | None = None,
        **_kwargs: Any,
    ) -> list[list[float]]:
        copied_texts: list[str] = []
        for text in texts:
            copied_texts.append(text)
        self.embedding_calls.append((config, copied_texts))

        vector_dimensions = dimensions
        if vector_dimensions is None:
            vector_dimensions = config.declared_dimensions
        if vector_dimensions is None:
            vector_dimensions = 3

        vectors: list[list[float]] = []
        for text_index in range(len(copied_texts)):
            vector: list[float] = []
            for dimension_index in range(vector_dimensions):
                value = (text_index + 1) * (dimension_index + 1)
                vector.append(float(value) / 100.0)
            vectors.append(vector)
        return vectors

    async def validate(
        self,
        collection_name: str,
        expected_count: int | None = None,
        expected_metadata: dict[str, Any] | None = None,
        expected_dimension: int | None = None,
        sample_size: int = 1,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        collection = self.collections.get(collection_name)
        if collection is None:
            return SimpleNamespace(
                exists=False,
                valid=False,
                count=None,
                metadata=None,
                sample_dimension=None,
                errors=("collection_not_found",),
            )

        items: dict[str, dict[str, Any]] = collection["items"]
        count = len(items)
        metadata = dict(collection["metadata"])
        errors: list[str] = []
        if expected_count is not None and count != expected_count:
            errors.append("count_mismatch")
        if expected_metadata is not None:
            for key, value in expected_metadata.items():
                if metadata.get(key) != value:
                    errors.append(f"metadata_mismatch:{key}")

        if sample_size <= 0:
            raise ValueError("sample_size must be positive")

        dimensions: set[int] = set()
        sampled = 0
        for item in items.values():
            if sampled >= sample_size:
                break
            sampled += 1
            vector = item.get("embedding")
            if vector is None:
                continue
            try:
                dimensions.add(len(vector))
            except TypeError:
                errors.append("sample_dimension_invalid")

        if len(dimensions) > 1:
            errors.append("sample_dimension_inconsistent")
        sample_dimension: int | None = None
        if dimensions:
            for dimension in dimensions:
                sample_dimension = dimension
                break
            if expected_dimension is not None and sample_dimension != expected_dimension:
                errors.append("dimension_mismatch")
        elif expected_dimension is not None:
            errors.append("sample_dimension_missing")

        return SimpleNamespace(
            exists=True,
            valid=not errors,
            count=count,
            metadata=metadata,
            sample_dimension=sample_dimension,
            errors=tuple(errors),
        )

    async def get_or_create_collection(
        self,
        collection_name: str,
        metadata: dict[str, Any] | None = None,
        distance: str | None = None,
    ) -> dict[str, Any]:
        collection = self.collections.get(collection_name)
        if collection is not None:
            return collection

        collection_metadata: dict[str, Any] = {}
        if metadata is not None:
            for key, value in metadata.items():
                collection_metadata[key] = value
        if distance is not None:
            collection_metadata["hnsw:space"] = distance
        collection = {"metadata": collection_metadata, "items": {}}
        self.collections[collection_name] = collection
        return collection

    async def delete_collection(self, collection_name: str) -> None:
        self.deleted_collections.append(collection_name)
        self.collections.pop(collection_name, None)

    async def upsert(
        self,
        collection_name: str,
        item_ids: Sequence[str],
        documents: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        **_kwargs: Any,
    ) -> int:
        ids: list[str] = []
        for item_id in item_ids:
            ids.append(item_id)
        document_values: list[str] = []
        for document in documents:
            document_values.append(document)
        vector_values: list[list[float]] = []
        for embedding in embeddings:
            vector: list[float] = []
            for value in embedding:
                vector.append(float(value))
            vector_values.append(vector)
        metadata_values: list[dict[str, Any]] = []
        for metadata in metadatas:
            metadata_values.append(dict(metadata))

        self.upsert_calls.append(
            {
                "collection_name": collection_name,
                "ids": ids,
                "documents": document_values,
                "embeddings": vector_values,
                "metadatas": metadata_values,
            }
        )

        collection = self.collections.get(collection_name)
        if collection is None:
            collection = {"metadata": {}, "items": {}}
            self.collections[collection_name] = collection
        if not (len(ids) == len(document_values) and len(ids) == len(vector_values) and len(ids) == len(metadata_values)):
            raise ValueError("vector item lengths do not match")
        for index, item_id in enumerate(ids):
            collection["items"][item_id] = {
                "document": document_values[index],
                "embedding": vector_values[index],
                "metadata": metadata_values[index],
            }
        return len(ids)

    async def get_items(
        self,
        collection_name: str,
        offset: int = 0,
        limit: int | None = None,
        include: list[str] | None = None,
    ) -> dict[str, list[Any]]:
        collection = self.collections.get(collection_name)
        result: dict[str, list[Any]] = {"ids": []}
        if include is None or "documents" in include:
            result["documents"] = []
        if include is None or "metadatas" in include:
            result["metadatas"] = []
        if include is not None and "embeddings" in include:
            result["embeddings"] = []
        if collection is None:
            return result

        selected = 0
        for item_id, item in collection["items"].items():
            if selected < offset:
                selected += 1
                continue
            if limit is not None and len(result["ids"]) >= limit:
                break
            result["ids"].append(item_id)
            if "documents" in result:
                result["documents"].append(item["document"])
            if "metadatas" in result:
                result["metadatas"].append(dict(item["metadata"]))
            if "embeddings" in result:
                vector: list[float] = []
                for value in item["embedding"]:
                    vector.append(float(value))
                result["embeddings"].append(vector)
        return result

    async def delete_orphans(
        self,
        collection_name: str,
        valid_item_ids: set[str],
        **_kwargs: Any,
    ) -> int:
        collection = self.collections.get(collection_name)
        if collection is None:
            return 0
        orphan_ids: list[str] = []
        for item_id in collection["items"]:
            if item_id not in valid_item_ids:
                orphan_ids.append(item_id)
        for item_id in orphan_ids:
            collection["items"].pop(item_id, None)
        return len(orphan_ids)

    async def delete_items(
        self,
        collection_name: str,
        item_ids: Sequence[str],
        **_kwargs: Any,
    ) -> int:
        ids: list[str] = []
        for item_id in item_ids:
            ids.append(item_id)
        self.deleted_items.append((collection_name, ids))
        collection = self.collections.get(collection_name)
        if collection is None:
            return len(ids)
        for item_id in ids:
            collection["items"].pop(item_id, None)
        return len(ids)

    async def hybrid_query(
        self,
        collection_name: str,
        _query_embedding: Sequence[float],
        _query: str,
        limit: int,
        **_kwargs: Any,
    ) -> list[SimpleNamespace]:
        hits: list[SimpleNamespace] = []
        if limit <= 0:
            return hits
        collection = self.collections.get(collection_name)
        if collection is None:
            return hits
        for item_id, item in collection["items"].items():
            hits.append(
                SimpleNamespace(
                    id=item_id,
                    content=item["document"],
                    metadata=dict(item["metadata"]),
                    dense_distance=0.0,
                    dense_rank=len(hits) + 1,
                )
            )
            if len(hits) >= limit:
                break
        return hits


def runtime_config(
    *,
    channel_id: int = 1,
    model_id: str = "memory-model-v1",
    dimensions: int = 3,
) -> EmbeddingRuntimeConfig:
    return EmbeddingRuntimeConfig(
        channel_id=channel_id,
        channel_name=f"channel-{channel_id}",
        model_id=model_id,
        declared_dimensions=dimensions,
        protocol="openai_embedding",
        timeout=30.0,
        base_url="https://embedding.invalid/v1",
        api_key="test-api-key",
    )


async def configure_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    channel_id: int = 1,
    model_id: str = "memory-model-v1",
    dimensions: int = 3,
    signature: str = "memory-signature-v1",
    active_revision: int = 1,
    index_revision: int = 1,
    collection_name: str = "memory-collection-v1",
    max_active_records: int = 50,
    revision: int | None = None,
) -> LongTermMemoryStore:
    if revision is not None:
        active_revision = revision
    async with session_factory() as db:
        return await memory_store_crud.create(
            db,
            uid=uid,
            active_embedding_channel_id=channel_id,
            active_embedding_model_id=model_id,
            active_embedding_dimensions=dimensions,
            active_embedding_signature=signature,
            active_embedding_revision=active_revision,
            active_collection_name=collection_name,
            max_active_records=max_active_records,
            index_revision=index_revision,
            index_status=LongTermMemoryIndexStatus.READY,
            migration_delta_high_watermark=0,
        )


async def create_recallable_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_key: str = "memory-key",
    content: str | None = None,
    version: int = 1,
    vector_item_id: str | None = None,
    content_hash: str | None = None,
    memory_type: LongTermMemoryType = LongTermMemoryType.FACT,
    source: LongTermMemorySource = LongTermMemorySource.USER_API,
    memory_id: int | None = None,
) -> LongTermMemoryRecord:
    if content is None:
        content = f"content-{memory_key}"
    if vector_item_id is None:
        vector_item_id = f"vector-{uid}-{memory_key}"
    if content_hash is None:
        content_hash = f"hash-{uid}-{memory_key}"
    values: dict[str, Any] = {
        "memory_key": memory_key,
        "memory_type": memory_type,
        "content": content,
        "content_hash": content_hash,
        "version": version,
        "indexed_version": version,
        "vector_item_id": vector_item_id,
        "source": source,
        "is_active": True,
        "suppress_recall": False,
        "index_status": LongTermMemoryRecordIndexStatus.READY,
        "indexed_at": get_local_time(),
    }
    if memory_id is not None:
        values["id"] = memory_id
    async with session_factory() as db:
        return await memory_record_crud.create(db, uid=uid, **values)


async def claim_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation | str,
    job_id: int | None = None,
    owner: str = "stage5-worker",
    worker_id: str | None = None,
    lease_seconds: int = 30,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    memory_id: int | None = None,
    expected_version: int | None = None,
    max_attempts: int = 3,
) -> LongTermMemoryMutationJob | None:
    normalized_operation = LongTermMemoryMutationOperation(operation)
    if worker_id is not None:
        owner = worker_id

    async with session_factory() as db:
        if job_id is None:
            if dedupe_key is None:
                dedupe_key = f"stage5-{normalized_operation.value}-{uuid4().hex}"
            available_at = await get_database_time(db)
            job, _created = await memory_job_crud.create(
                db,
                uid=uid,
                operation=normalized_operation,
                dedupe_key=dedupe_key,
                payload=payload or {},
                memory_id=memory_id,
                expected_version=expected_version,
                max_attempts=max_attempts,
                available_at=available_at,
            )
            job_id = job.id
        if job_id is None:
            return None
        enabled_operations: list[LongTermMemoryMutationOperation] = []
        enabled_operations.append(normalized_operation)
        return await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            lease_seconds=lease_seconds,
            enabled_operations=enabled_operations,
        )


__all__ = (
    "MEMORY_TABLES",
    "Stage5VectorBackend",
    "claim_job",
    "configure_store",
    "create_recallable_record",
    "runtime_config",
)
