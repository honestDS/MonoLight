from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.knowledge_base import (
    KnowledgeBaseDocument,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseMigrationDeltaStatus,
    KnowledgeBaseMigrationSourceType,
    ManagedKnowledgeItem,
)


@dataclass(frozen=True, slots=True)
class KnowledgeMigrationSnapshotBoundary:
    document_max_id: int
    managed_max_id: int
    total_count: int

    @property
    def logical_boundary(self) -> int:
        return self.document_max_id + self.managed_max_id


@dataclass(frozen=True, slots=True)
class KnowledgeMigrationSnapshotRecord:
    source_type: KnowledgeBaseMigrationSourceType
    source_id: int
    logical_cursor: int
    value: KnowledgeBaseDocument | ManagedKnowledgeItem


class CRUDKnowledgeBaseMigration:
    async def get_snapshot_boundary(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> KnowledgeMigrationSnapshotBoundary:
        document_max_id = int(
            (
                await db.scalar(
                    select(func.max(KnowledgeBaseDocument.id)).where(
                        KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id,
                    )
                )
            )
            or 0
        )
        managed_conditions = (
            ManagedKnowledgeItem.uid == uid,
            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            ManagedKnowledgeItem.is_recallable.is_(True),
            ManagedKnowledgeItem.deleted_at.is_(None),
        )
        managed_max_id = int((await db.scalar(select(func.max(ManagedKnowledgeItem.id)).where(*managed_conditions))) or 0)
        document_count = int((await db.scalar(select(func.count()).select_from(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id))) or 0)
        managed_count = int((await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem).where(*managed_conditions))) or 0)
        return KnowledgeMigrationSnapshotBoundary(
            document_max_id=document_max_id,
            managed_max_id=managed_max_id,
            total_count=document_count + managed_count,
        )

    async def list_snapshot_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        document_max_id: int,
        managed_max_id: int,
        cursor: int,
        limit: int,
    ) -> list[KnowledgeMigrationSnapshotRecord]:
        if limit <= 0:
            return []
        records: list[KnowledgeMigrationSnapshotRecord] = []
        if cursor < document_max_id:
            documents = list(
                (
                    await db.execute(
                        select(KnowledgeBaseDocument)
                        .where(
                            KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id,
                            KnowledgeBaseDocument.id > cursor,
                            KnowledgeBaseDocument.id <= document_max_id,
                        )
                        .order_by(KnowledgeBaseDocument.id.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            records.extend(
                KnowledgeMigrationSnapshotRecord(
                    source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
                    source_id=int(document.id),
                    logical_cursor=int(document.id),
                    value=document,
                )
                for document in documents
                if document.id is not None
            )
            if len(records) >= limit:
                return records

        managed_cursor = max(cursor - document_max_id, 0)
        if cursor < document_max_id:
            managed_cursor = 0
        remaining = limit - len(records)
        if remaining > 0 and managed_cursor < managed_max_id:
            managed_items = list(
                (
                    await db.execute(
                        select(ManagedKnowledgeItem)
                        .where(
                            ManagedKnowledgeItem.uid == uid,
                            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                            ManagedKnowledgeItem.id > managed_cursor,
                            ManagedKnowledgeItem.id <= managed_max_id,
                            ManagedKnowledgeItem.is_recallable.is_(True),
                            ManagedKnowledgeItem.deleted_at.is_(None),
                        )
                        .order_by(ManagedKnowledgeItem.id.asc())
                        .limit(remaining)
                    )
                )
                .scalars()
                .all()
            )
            records.extend(
                KnowledgeMigrationSnapshotRecord(
                    source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
                    source_id=int(item.id),
                    logical_cursor=document_max_id + int(item.id),
                    value=item,
                )
                for item in managed_items
                if item.id is not None
            )
        return records

    async def list_current_sources(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> list[KnowledgeMigrationSnapshotRecord]:
        documents = list((await db.execute(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id).order_by(KnowledgeBaseDocument.id.asc()))).scalars().all())
        managed_items = list(
            (
                await db.execute(
                    select(ManagedKnowledgeItem)
                    .where(
                        ManagedKnowledgeItem.uid == uid,
                        ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                        ManagedKnowledgeItem.is_recallable.is_(True),
                        ManagedKnowledgeItem.deleted_at.is_(None),
                    )
                    .order_by(ManagedKnowledgeItem.id.asc())
                )
            )
            .scalars()
            .all()
        )
        document_max_id = max((int(item.id) for item in documents if item.id is not None), default=0)
        records = [
            KnowledgeMigrationSnapshotRecord(
                source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
                source_id=int(document.id),
                logical_cursor=int(document.id),
                value=document,
            )
            for document in documents
            if document.id is not None
        ]
        records.extend(
            KnowledgeMigrationSnapshotRecord(
                source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
                source_id=int(item.id),
                logical_cursor=document_max_id + int(item.id),
                value=item,
            )
            for item in managed_items
            if item.id is not None
        )
        return records

    async def get_source(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        source_type: KnowledgeBaseMigrationSourceType,
        source_id: int,
    ) -> KnowledgeBaseDocument | ManagedKnowledgeItem | None:
        if source_type == KnowledgeBaseMigrationSourceType.USER_DOCUMENT:
            return await db.scalar(
                select(KnowledgeBaseDocument).where(
                    KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseDocument.id == source_id,
                )
            )
        return await db.scalar(
            select(ManagedKnowledgeItem).where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == source_id,
                ManagedKnowledgeItem.is_recallable.is_(True),
                ManagedKnowledgeItem.deleted_at.is_(None),
            )
        )

    async def create_delta(
        self,
        db: AsyncSession,
        *,
        delta: KnowledgeBaseEmbeddingDelta,
    ) -> KnowledgeBaseEmbeddingDelta:
        db.add(delta)
        await db.flush()
        await db.refresh(delta)
        return delta

    async def list_deltas(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        sequence_start: int,
        sequence_end: int,
        limit: int,
    ) -> list[KnowledgeBaseEmbeddingDelta]:
        result = await db.execute(
            select(KnowledgeBaseEmbeddingDelta)
            .where(
                KnowledgeBaseEmbeddingDelta.uid == uid,
                KnowledgeBaseEmbeddingDelta.migration_job_id == migration_job_id,
                KnowledgeBaseEmbeddingDelta.sequence >= sequence_start,
                KnowledgeBaseEmbeddingDelta.sequence <= sequence_end,
            )
            .order_by(KnowledgeBaseEmbeddingDelta.sequence.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_delta_applied(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        sequence: int,
        applied_at: datetime,
    ) -> bool:
        result = await db.execute(
            update(KnowledgeBaseEmbeddingDelta)
            .where(
                KnowledgeBaseEmbeddingDelta.uid == uid,
                KnowledgeBaseEmbeddingDelta.migration_job_id == migration_job_id,
                KnowledgeBaseEmbeddingDelta.sequence == sequence,
                KnowledgeBaseEmbeddingDelta.status.in_(
                    [
                        KnowledgeBaseMigrationDeltaStatus.PENDING,
                        KnowledgeBaseMigrationDeltaStatus.APPLIED,
                    ]
                ),
            )
            .values(
                status=KnowledgeBaseMigrationDeltaStatus.APPLIED,
                error=None,
                applied_at=applied_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return (result.rowcount or 0) == 1

    async def update_document_vectors_batch(
        self,
        db: AsyncSession,
        *,
        knowledge_base_id: int,
        updates: list[tuple[int, list[str]]],
    ) -> bool:
        if not updates:
            return True
        statement = (
            update(KnowledgeBaseDocument.__table__)
            .where(
                KnowledgeBaseDocument.__table__.c.knowledge_base_id == bindparam("batch_knowledge_base_id"),
                KnowledgeBaseDocument.__table__.c.id == bindparam("batch_document_id"),
            )
            .values(
                chunk_ids=bindparam("batch_chunk_ids"),
                chunk_count=bindparam("batch_chunk_count"),
            )
        )
        result = await db.execute(
            statement,
            [
                {
                    "batch_knowledge_base_id": knowledge_base_id,
                    "batch_document_id": document_id,
                    "batch_chunk_ids": list(chunk_ids),
                    "batch_chunk_count": len(chunk_ids),
                }
                for document_id, chunk_ids in updates
            ],
        )
        await db.flush()
        return result.rowcount == len(updates)

    async def update_managed_vectors_batch(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        updates: list[tuple[int, int, list[str]]],
    ) -> bool:
        if not updates:
            return True
        statement = (
            update(ManagedKnowledgeItem.__table__)
            .where(
                ManagedKnowledgeItem.__table__.c.uid == bindparam("batch_uid"),
                ManagedKnowledgeItem.__table__.c.knowledge_base_id == bindparam("batch_knowledge_base_id"),
                ManagedKnowledgeItem.__table__.c.id == bindparam("batch_knowledge_id"),
                ManagedKnowledgeItem.__table__.c.version == bindparam("batch_version"),
                ManagedKnowledgeItem.__table__.c.is_recallable.is_(True),
                ManagedKnowledgeItem.__table__.c.deleted_at.is_(None),
            )
            .values(
                indexed_version=bindparam("batch_indexed_version"),
                vector_item_ids=bindparam("batch_vector_item_ids"),
            )
        )
        result = await db.execute(
            statement,
            [
                {
                    "batch_uid": uid,
                    "batch_knowledge_base_id": knowledge_base_id,
                    "batch_knowledge_id": knowledge_id,
                    "batch_version": version,
                    "batch_indexed_version": version,
                    "batch_vector_item_ids": list(vector_item_ids),
                }
                for knowledge_id, version, vector_item_ids in updates
            ],
        )
        await db.flush()
        return result.rowcount == len(updates)


knowledge_base_migration_crud = CRUDKnowledgeBaseMigration()


__all__ = [
    "KnowledgeMigrationSnapshotBoundary",
    "KnowledgeMigrationSnapshotRecord",
    "knowledge_base_migration_crud",
]
