import hashlib

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryFragmentStatus,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)


def build_context_summary_fragment_dedupe_key(
    *,
    work_dedupe_key: str,
    stage_key: str,
    model_key: str,
    fragment_index: int,
) -> str:
    identity = "\x1f".join(
        (
            work_dedupe_key,
            stage_key,
            model_key,
            str(fragment_index),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class CRUDContextSummaryFragment(
    CRUDBase[
        ContextSummaryFragment,
        ContextSummaryFragment,
        ContextSummaryFragment,
    ]
):
    async def get_by_dedupe_key(
        self,
        db: AsyncSession,
        *,
        dedupe_key: str,
    ) -> ContextSummaryFragment | None:
        result = await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def write_ordered(
        self,
        db: AsyncSession,
        *,
        fragment: ContextSummaryFragment,
    ) -> tuple[ContextSummaryFragment | None, bool]:
        expected_dedupe_key = build_context_summary_fragment_dedupe_key(
            work_dedupe_key=fragment.work_dedupe_key,
            stage_key=fragment.stage_key,
            model_key=fragment.model_key,
            fragment_index=fragment.fragment_index,
        )
        if fragment.dedupe_key != expected_dedupe_key:
            return None, False

        existing = await self.get_by_dedupe_key(
            db,
            dedupe_key=fragment.dedupe_key,
        )
        if existing is not None:
            if existing.work_dedupe_key == fragment.work_dedupe_key and existing.stage_key == fragment.stage_key and existing.model_key == fragment.model_key and existing.fragment_index == fragment.fragment_index:
                return existing, False
            return None, False

        if fragment.status != ContextSummaryFragmentStatus.COMPLETED:
            return None, False

        stage_result = await db.execute(
            update(ContextSummaryStage)
            .where(
                ContextSummaryStage.uid == fragment.uid,
                ContextSummaryStage.session_id == fragment.session_id,
                ContextSummaryStage.work_id == fragment.work_id,
                ContextSummaryStage.work_dedupe_key == fragment.work_dedupe_key,
                ContextSummaryStage.snapshot_key == fragment.snapshot_key,
                ContextSummaryStage.stage_key == fragment.stage_key,
                ContextSummaryStage.model_key == fragment.model_key,
                ContextSummaryStage.channel_id == fragment.channel_id,
                ContextSummaryStage.model_id == fragment.model_id,
                ContextSummaryStage.status == ContextSummaryStageStatus.RUNNING,
                ContextSummaryStage.succeeded_fragment_count == fragment.fragment_index,
                ContextSummaryStage.succeeded_fragment_count < ContextSummaryStage.expected_fragment_count,
            )
            .values(succeeded_fragment_count=(ContextSummaryStage.succeeded_fragment_count + 1))
            .execution_options(synchronize_session=False)
        )
        if stage_result.rowcount != 1:
            await db.rollback()
            existing = await self.get_by_dedupe_key(
                db,
                dedupe_key=fragment.dedupe_key,
            )
            if existing is not None:
                return existing, False
            return None, False

        db.add(fragment)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_dedupe_key(
                db,
                dedupe_key=fragment.dedupe_key,
            )
            if existing is None:
                return None, False
            return existing, False

        await db.refresh(fragment)
        return fragment, True


context_summary_fragment_crud = CRUDContextSummaryFragment(ContextSummaryFragment)
