from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import delete, exists, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_KNOWLEDGE_ORGANIZATION_STAGE_IDENTITY_CONFLICT, ERR_VALUE_MUST_BE_BETWEEN
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.knowledge_base import (
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
)

KNOWLEDGE_ORGANIZATION_CLEANUP_BATCH_SIZE = 200
KNOWLEDGE_ORGANIZATION_CLEANUP_MAX_BATCH_SIZE = 1000


def _validate_batch_size(batch_size: int) -> None:
    if not 1 <= batch_size <= KNOWLEDGE_ORGANIZATION_CLEANUP_MAX_BATCH_SIZE:
        raise ValueError(
            t(
                ERR_VALUE_MUST_BE_BETWEEN,
                field="batch_size",
                minimum=1,
                maximum=KNOWLEDGE_ORGANIZATION_CLEANUP_MAX_BATCH_SIZE,
            )
        )


class CRUDKnowledgeOrganizationSnapshot:
    async def get_by_identity(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        snapshot_key: str,
    ) -> KnowledgeOrganizationSnapshot | None:
        result = await db.execute(
            select(KnowledgeOrganizationSnapshot).where(
                KnowledgeOrganizationSnapshot.uid == uid,
                KnowledgeOrganizationSnapshot.knowledge_base_id == knowledge_base_id,
                KnowledgeOrganizationSnapshot.snapshot_key == snapshot_key,
            )
        )
        return result.scalars().first()

    async def create_snapshot(
        self,
        db: AsyncSession,
        *,
        snapshot: KnowledgeOrganizationSnapshot,
    ) -> tuple[KnowledgeOrganizationSnapshot, bool]:
        db.add(snapshot)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_identity(
                db,
                uid=snapshot.uid,
                knowledge_base_id=snapshot.knowledge_base_id,
                snapshot_key=snapshot.snapshot_key,
            )
            if existing is None:
                raise
            return existing, False
        await db.refresh(snapshot)
        return snapshot, True


class CRUDKnowledgeOrganizationFragment:
    @staticmethod
    def build_dedupe_key(
        *,
        work_key: str,
        stage_key: str,
        model_key: str,
        fragment_index: int,
    ) -> str:
        identity = "\x1f".join((work_key, stage_key, model_key, str(fragment_index)))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def get_by_dedupe_key(
        self,
        db: AsyncSession,
        *,
        dedupe_key: str,
    ) -> KnowledgeOrganizationFragment | None:
        result = await db.execute(
            select(KnowledgeOrganizationFragment).where(
                KnowledgeOrganizationFragment.dedupe_key == dedupe_key,
            )
        )
        return result.scalars().first()

    @staticmethod
    def _matches_existing(
        existing: KnowledgeOrganizationFragment,
        fragment: KnowledgeOrganizationFragment,
    ) -> bool:
        return (
            existing.dedupe_key == fragment.dedupe_key
            and existing.uid == fragment.uid
            and existing.knowledge_base_id == fragment.knowledge_base_id
            and existing.stage_id == fragment.stage_id
            and existing.snapshot_id == fragment.snapshot_id
            and existing.work_key == fragment.work_key
            and existing.snapshot_key == fragment.snapshot_key
            and existing.stage_key == fragment.stage_key
            and existing.model_key == fragment.model_key
            and existing.fragment_index == fragment.fragment_index
            and existing.candidate_scope == fragment.candidate_scope
            and existing.result == fragment.result
            and existing.status == fragment.status
        )

    async def write_ordered(
        self,
        db: AsyncSession,
        *,
        fragment: KnowledgeOrganizationFragment,
    ) -> tuple[KnowledgeOrganizationFragment | None, bool]:
        expected_dedupe_key = self.build_dedupe_key(
            work_key=fragment.work_key,
            stage_key=fragment.stage_key,
            model_key=fragment.model_key,
            fragment_index=fragment.fragment_index,
        )
        if fragment.dedupe_key != expected_dedupe_key or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED:
            return None, False

        existing = await self.get_by_dedupe_key(db, dedupe_key=fragment.dedupe_key)
        if existing is not None:
            return (existing, False) if self._matches_existing(existing, fragment) else (None, False)

        stage_result = await db.execute(
            update(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.id == fragment.stage_id,
                KnowledgeOrganizationStage.uid == fragment.uid,
                KnowledgeOrganizationStage.knowledge_base_id == fragment.knowledge_base_id,
                KnowledgeOrganizationStage.snapshot_id == fragment.snapshot_id,
                KnowledgeOrganizationStage.work_key == fragment.work_key,
                KnowledgeOrganizationStage.snapshot_key == fragment.snapshot_key,
                KnowledgeOrganizationStage.stage_key == fragment.stage_key,
                KnowledgeOrganizationStage.model_key == fragment.model_key,
                KnowledgeOrganizationStage.status == KnowledgeOrganizationStageStatus.RUNNING,
                KnowledgeOrganizationStage.succeeded_fragment_count == fragment.fragment_index,
                KnowledgeOrganizationStage.succeeded_fragment_count < KnowledgeOrganizationStage.expected_fragment_count,
            )
            .values(succeeded_fragment_count=KnowledgeOrganizationStage.succeeded_fragment_count + 1)
            .execution_options(synchronize_session=False)
        )
        if (stage_result.rowcount or 0) != 1:
            await db.rollback()
            existing = await self.get_by_dedupe_key(db, dedupe_key=fragment.dedupe_key)
            if existing is None or not self._matches_existing(existing, fragment):
                return None, False
            return existing, False

        db.add(fragment)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_dedupe_key(db, dedupe_key=fragment.dedupe_key)
            if existing is None or not self._matches_existing(existing, fragment):
                return None, False
            return existing, False
        await db.refresh(fragment)
        return fragment, True


class CRUDKnowledgeOrganizationStage:
    async def get_by_identity(
        self,
        db: AsyncSession,
        *,
        work_key: str,
        stage_key: str,
    ) -> KnowledgeOrganizationStage | None:
        result = await db.execute(
            select(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.work_key == work_key,
                KnowledgeOrganizationStage.stage_key == stage_key,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def create_stage(
        self,
        db: AsyncSession,
        *,
        stage: KnowledgeOrganizationStage,
    ) -> tuple[KnowledgeOrganizationStage, bool]:
        stage_identity = {
            "uid": stage.uid,
            "knowledge_base_id": stage.knowledge_base_id,
            "snapshot_id": stage.snapshot_id,
            "work_key": stage.work_key,
            "snapshot_key": stage.snapshot_key,
            "stage_key": stage.stage_key,
            "stage_index": stage.stage_index,
            "lower_stage_key": stage.lower_stage_key,
            "model_key": stage.model_key,
            "model_snapshot": dict(stage.model_snapshot),
            "expected_fragment_count": stage.expected_fragment_count,
        }
        db.add(stage)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_identity(
                db,
                work_key=stage_identity["work_key"],
                stage_key=stage_identity["stage_key"],
            )
            if existing is None:
                raise
            if any(
                (
                    existing.uid != stage_identity["uid"],
                    existing.knowledge_base_id != stage_identity["knowledge_base_id"],
                    existing.snapshot_id != stage_identity["snapshot_id"],
                    existing.snapshot_key != stage_identity["snapshot_key"],
                    existing.stage_index != stage_identity["stage_index"],
                    existing.lower_stage_key != stage_identity["lower_stage_key"],
                    existing.model_key != stage_identity["model_key"],
                    existing.model_snapshot != stage_identity["model_snapshot"],
                    existing.expected_fragment_count != stage_identity["expected_fragment_count"],
                )
            ):
                raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_STAGE_IDENTITY_CONFLICT))
            return existing, False
        await db.refresh(stage)
        return stage, True

    async def get_resume_fragment_index(
        self,
        db: AsyncSession,
        *,
        work_key: str,
        stage_key: str,
        snapshot_key: str,
        model_key: str,
    ) -> int | None:
        stage = await self.get_by_identity(db, work_key=work_key, stage_key=stage_key)
        if stage is None or stage.snapshot_key != snapshot_key or stage.model_key != model_key or stage.status not in {KnowledgeOrganizationStageStatus.RUNNING, KnowledgeOrganizationStageStatus.COMPLETED} or stage.succeeded_fragment_count > stage.expected_fragment_count:
            return None

        fragments = list((await db.execute(select(KnowledgeOrganizationFragment).where(KnowledgeOrganizationFragment.stage_id == stage.id).order_by(KnowledgeOrganizationFragment.fragment_index))).scalars().all())
        for expected_index, fragment in enumerate(fragments):
            if (
                fragment.fragment_index != expected_index
                or fragment.uid != stage.uid
                or fragment.knowledge_base_id != stage.knowledge_base_id
                or fragment.snapshot_id != stage.snapshot_id
                or fragment.work_key != stage.work_key
                or fragment.snapshot_key != stage.snapshot_key
                or fragment.stage_key != stage.stage_key
                or fragment.model_key != stage.model_key
                or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED
            ):
                return None
        if len(fragments) != stage.succeeded_fragment_count:
            return None
        if stage.status == KnowledgeOrganizationStageStatus.COMPLETED and len(fragments) != stage.expected_fragment_count:
            return None
        return stage.succeeded_fragment_count

    async def mark_completed(
        self,
        db: AsyncSession,
        *,
        work_key: str,
        stage_key: str,
        snapshot_key: str,
        model_key: str,
    ) -> bool:
        stage_result = await db.execute(
            select(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.work_key == work_key,
                KnowledgeOrganizationStage.stage_key == stage_key,
                KnowledgeOrganizationStage.snapshot_key == snapshot_key,
                KnowledgeOrganizationStage.model_key == model_key,
                KnowledgeOrganizationStage.status == KnowledgeOrganizationStageStatus.RUNNING,
            )
            .with_for_update()
        )
        stage = stage_result.scalars().first()
        if stage is None or stage.succeeded_fragment_count != stage.expected_fragment_count:
            await db.rollback()
            return False

        fragments = list((await db.execute(select(KnowledgeOrganizationFragment).where(KnowledgeOrganizationFragment.stage_id == stage.id).order_by(KnowledgeOrganizationFragment.fragment_index))).scalars().all())
        if len(fragments) != stage.expected_fragment_count:
            await db.rollback()
            return False
        for expected_index, fragment in enumerate(fragments):
            if (
                fragment.fragment_index != expected_index
                or fragment.uid != stage.uid
                or fragment.knowledge_base_id != stage.knowledge_base_id
                or fragment.snapshot_id != stage.snapshot_id
                or fragment.work_key != stage.work_key
                or fragment.snapshot_key != stage.snapshot_key
                or fragment.stage_key != stage.stage_key
                or fragment.model_key != stage.model_key
                or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED
            ):
                await db.rollback()
                return False

        now = get_local_time()
        completed = await db.execute(
            update(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.id == stage.id,
                KnowledgeOrganizationStage.status == KnowledgeOrganizationStageStatus.RUNNING,
                KnowledgeOrganizationStage.succeeded_fragment_count == KnowledgeOrganizationStage.expected_fragment_count,
            )
            .values(
                status=KnowledgeOrganizationStageStatus.COMPLETED,
                completed_at=now,
                error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if (completed.rowcount or 0) != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        work_key: str,
        stage_key: str,
        snapshot_key: str,
        model_key: str,
        error: str,
    ) -> bool:
        result = await db.execute(
            update(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.work_key == work_key,
                KnowledgeOrganizationStage.stage_key == stage_key,
                KnowledgeOrganizationStage.snapshot_key == snapshot_key,
                KnowledgeOrganizationStage.model_key == model_key,
                KnowledgeOrganizationStage.status == KnowledgeOrganizationStageStatus.RUNNING,
            )
            .values(
                status=KnowledgeOrganizationStageStatus.FAILED,
                error=error,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def invalidate(
        self,
        db: AsyncSession,
        *,
        work_key: str,
        stage_key: str,
        snapshot_key: str,
        model_key: str,
    ) -> bool:
        stage_result = await db.execute(
            update(KnowledgeOrganizationStage)
            .where(
                KnowledgeOrganizationStage.work_key == work_key,
                KnowledgeOrganizationStage.stage_key == stage_key,
                KnowledgeOrganizationStage.snapshot_key == snapshot_key,
                KnowledgeOrganizationStage.model_key == model_key,
                KnowledgeOrganizationStage.status.in_(
                    (
                        KnowledgeOrganizationStageStatus.RUNNING,
                        KnowledgeOrganizationStageStatus.FAILED,
                    )
                ),
            )
            .values(
                status=KnowledgeOrganizationStageStatus.INVALIDATED,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if (stage_result.rowcount or 0) != 1:
            await db.rollback()
            return False

        await db.execute(
            update(KnowledgeOrganizationFragment)
            .where(
                KnowledgeOrganizationFragment.work_key == work_key,
                KnowledgeOrganizationFragment.snapshot_key == snapshot_key,
                KnowledgeOrganizationFragment.stage_key == stage_key,
                KnowledgeOrganizationFragment.model_key == model_key,
                KnowledgeOrganizationFragment.status == KnowledgeOrganizationFragmentStatus.COMPLETED,
            )
            .values(status=KnowledgeOrganizationFragmentStatus.INVALIDATED)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return True

    async def cleanup_expired(
        self,
        db: AsyncSession,
        *,
        before: datetime,
        batch_size: int = KNOWLEDGE_ORGANIZATION_CLEANUP_BATCH_SIZE,
    ) -> int:
        _validate_batch_size(batch_size)

        fragment_ids = list((await db.execute(select(KnowledgeOrganizationFragment.id).where(KnowledgeOrganizationFragment.created_at < before).order_by(KnowledgeOrganizationFragment.id).limit(batch_size))).scalars().all())
        if fragment_ids:
            result = await db.execute(delete(KnowledgeOrganizationFragment).where(KnowledgeOrganizationFragment.id.in_(fragment_ids)).execution_options(synchronize_session=False))
            await db.commit()
            return result.rowcount or 0

        fragment_exists = exists().where(KnowledgeOrganizationFragment.stage_id == KnowledgeOrganizationStage.id)
        stage_ids = list(
            (
                await db.execute(
                    select(KnowledgeOrganizationStage.id)
                    .where(
                        KnowledgeOrganizationStage.created_at < before,
                        ~fragment_exists,
                    )
                    .order_by(KnowledgeOrganizationStage.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if stage_ids:
            result = await db.execute(delete(KnowledgeOrganizationStage).where(KnowledgeOrganizationStage.id.in_(stage_ids)).execution_options(synchronize_session=False))
            await db.commit()
            return result.rowcount or 0

        stage_exists = exists().where(KnowledgeOrganizationStage.snapshot_id == KnowledgeOrganizationSnapshot.id)
        snapshot_ids = list(
            (
                await db.execute(
                    select(KnowledgeOrganizationSnapshot.id)
                    .where(
                        KnowledgeOrganizationSnapshot.created_at < before,
                        ~stage_exists,
                    )
                    .order_by(KnowledgeOrganizationSnapshot.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not snapshot_ids:
            return 0
        result = await db.execute(delete(KnowledgeOrganizationSnapshot).where(KnowledgeOrganizationSnapshot.id.in_(snapshot_ids)).execution_options(synchronize_session=False))
        await db.commit()
        return result.rowcount or 0


knowledge_organization_snapshot_crud = CRUDKnowledgeOrganizationSnapshot()
knowledge_organization_fragment_crud = CRUDKnowledgeOrganizationFragment()
knowledge_organization_stage_crud = CRUDKnowledgeOrganizationStage()
