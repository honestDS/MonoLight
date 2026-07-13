import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryFragmentStatus,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)
from app.models.message import Message

CONTEXT_SUMMARY_CLEANUP_BATCH_SIZE = 200
CONTEXT_SUMMARY_CLEANUP_MAX_BATCH_SIZE = 1000
CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE = 200
CONTEXT_SUMMARY_FRAGMENT_MAX_PAGE_SIZE = 500


@dataclass(frozen=True)
class CompletedContextSummaryFragmentPage:
    stage: ContextSummaryStage
    fragments: tuple[ContextSummaryFragment, ...]


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


def _validate_batch_size(batch_size: int) -> None:
    if not 1 <= batch_size <= CONTEXT_SUMMARY_CLEANUP_MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {CONTEXT_SUMMARY_CLEANUP_MAX_BATCH_SIZE}")


class CRUDContextSummaryStage(CRUDBase[ContextSummaryStage, ContextSummaryStage, ContextSummaryStage]):
    async def get_by_identity(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        stage_key: str,
    ) -> ContextSummaryStage | None:
        result = await db.execute(
            select(ContextSummaryStage).where(
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == stage_key,
            )
        )
        return result.scalars().first()

    async def get_completed_fragment_page(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        lower_stage_key: str,
        page_after_message_id: int | None = None,
        limit: int = CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE,
    ) -> CompletedContextSummaryFragmentPage | None:
        if not 1 <= limit <= CONTEXT_SUMMARY_FRAGMENT_MAX_PAGE_SIZE:
            raise ValueError(
                f"limit must be between 1 and {CONTEXT_SUMMARY_FRAGMENT_MAX_PAGE_SIZE}",
            )
        if page_after_message_id is not None and page_after_message_id < 1:
            raise ValueError("page_after_message_id must be positive")

        stage_result = await db.execute(
            select(ContextSummaryStage).where(
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == lower_stage_key,
                ContextSummaryStage.status == ContextSummaryStageStatus.COMPLETED,
                ContextSummaryStage.succeeded_fragment_count == ContextSummaryStage.expected_fragment_count,
            )
        )
        stage = stage_result.scalars().first()
        if stage is None:
            return None

        fragment_query = select(ContextSummaryFragment).where(
            ContextSummaryFragment.uid == stage.uid,
            ContextSummaryFragment.session_id == stage.session_id,
            ContextSummaryFragment.work_id == stage.work_id,
            ContextSummaryFragment.work_dedupe_key == stage.work_dedupe_key,
            ContextSummaryFragment.snapshot_key == stage.snapshot_key,
            ContextSummaryFragment.stage_key == stage.stage_key,
            ContextSummaryFragment.model_key == stage.model_key,
            ContextSummaryFragment.channel_id == stage.channel_id,
            ContextSummaryFragment.model_id == stage.model_id,
            ContextSummaryFragment.status == ContextSummaryFragmentStatus.COMPLETED,
        )
        if page_after_message_id is not None:
            fragment_query = fragment_query.where(
                ContextSummaryFragment.message_start_id > page_after_message_id,
            )

        fragment_result = await db.execute(
            fragment_query.order_by(
                ContextSummaryFragment.message_start_id,
                ContextSummaryFragment.fragment_index,
            ).limit(limit)
        )
        return CompletedContextSummaryFragmentPage(
            stage=stage,
            fragments=tuple(fragment_result.scalars().all()),
        )

    async def create_stage(
        self,
        db: AsyncSession,
        *,
        stage: ContextSummaryStage,
    ) -> tuple[ContextSummaryStage, bool]:
        db.add(stage)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_identity(
                db,
                work_dedupe_key=stage.work_dedupe_key,
                stage_key=stage.stage_key,
            )
            if existing is None:
                raise
            return existing, False
        await db.refresh(stage)
        return stage, True

    async def mark_completed(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        stage_key: str,
        model_key: str,
    ) -> bool:
        stage_result = await db.execute(
            select(ContextSummaryStage)
            .where(
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == stage_key,
                ContextSummaryStage.model_key == model_key,
                ContextSummaryStage.status == ContextSummaryStageStatus.RUNNING,
            )
            .with_for_update()
        )
        stage = stage_result.scalars().first()
        if stage is None or stage.succeeded_fragment_count != stage.expected_fragment_count:
            await db.rollback()
            return False

        fragment_result = await db.stream_scalars(
            select(ContextSummaryFragment)
            .where(
                ContextSummaryFragment.work_dedupe_key == work_dedupe_key,
                ContextSummaryFragment.stage_key == stage_key,
            )
            .order_by(ContextSummaryFragment.fragment_index)
        )
        fragment_count = 0
        previous_message_end_id: int | None = None
        async for fragment in fragment_result:
            if (
                fragment.fragment_index != fragment_count
                or fragment.uid != stage.uid
                or fragment.session_id != stage.session_id
                or fragment.work_id != stage.work_id
                or fragment.snapshot_key != stage.snapshot_key
                or fragment.model_key != stage.model_key
                or fragment.channel_id != stage.channel_id
                or fragment.model_id != stage.model_id
                or fragment.status != ContextSummaryFragmentStatus.COMPLETED
                or fragment.message_start_id > fragment.message_end_id
                or (previous_message_end_id is not None and fragment.message_start_id <= previous_message_end_id)
            ):
                await db.rollback()
                return False
            if fragment_count == 0 and fragment.message_start_id <= (stage.expected_summary_message_id or 0):
                await db.rollback()
                return False
            previous_message_end_id = fragment.message_end_id
            fragment_count += 1

        if fragment_count != stage.expected_fragment_count or previous_message_end_id != stage.persistent_summary_target_id:
            await db.rollback()
            return False

        fragment_identity = (
            ContextSummaryFragment.work_dedupe_key == work_dedupe_key,
            ContextSummaryFragment.stage_key == stage_key,
            ContextSummaryFragment.model_key == model_key,
            ContextSummaryFragment.status == ContextSummaryFragmentStatus.COMPLETED,
        )
        start_message_exists = exists().where(
            Message.id == ContextSummaryFragment.message_start_id,
            Message.uid == stage.uid,
            Message.session_id == stage.session_id,
        )
        end_message_exists = exists().where(
            Message.id == ContextSummaryFragment.message_end_id,
            Message.uid == stage.uid,
            Message.session_id == stage.session_id,
        )
        invalid_endpoint = (
            await db.execute(
                select(ContextSummaryFragment.id)
                .where(
                    *fragment_identity,
                    or_(~start_message_exists, ~end_message_exists),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if invalid_endpoint is not None:
            await db.rollback()
            return False

        covered_by_fragment = exists().where(
            *fragment_identity,
            ContextSummaryFragment.message_start_id <= Message.id,
            ContextSummaryFragment.message_end_id >= Message.id,
        )
        missing_message = (
            await db.execute(
                select(Message.id)
                .where(
                    Message.uid == stage.uid,
                    Message.session_id == stage.session_id,
                    Message.id > (stage.expected_summary_message_id or 0),
                    Message.id <= stage.persistent_summary_target_id,
                    ~covered_by_fragment,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if missing_message is not None:
            await db.rollback()
            return False

        now = get_local_time()
        completed_result = await db.execute(
            update(ContextSummaryStage)
            .where(
                ContextSummaryStage.id == stage.id,
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == stage_key,
                ContextSummaryStage.model_key == model_key,
                ContextSummaryStage.status == ContextSummaryStageStatus.RUNNING,
                ContextSummaryStage.succeeded_fragment_count == ContextSummaryStage.expected_fragment_count,
            )
            .values(
                status=ContextSummaryStageStatus.COMPLETED,
                completed_at=now,
                error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if completed_result.rowcount != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        stage_key: str,
        model_key: str,
        error: str,
    ) -> bool:
        result = await db.execute(
            update(ContextSummaryStage)
            .where(
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == stage_key,
                ContextSummaryStage.model_key == model_key,
                ContextSummaryStage.status == ContextSummaryStageStatus.RUNNING,
            )
            .values(
                status=ContextSummaryStageStatus.FAILED,
                error=error,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def invalidate(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        stage_key: str,
        model_key: str,
    ) -> bool:
        stage_result = await db.execute(
            update(ContextSummaryStage)
            .where(
                ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                ContextSummaryStage.stage_key == stage_key,
                ContextSummaryStage.model_key == model_key,
                ContextSummaryStage.status.in_(
                    (
                        ContextSummaryStageStatus.RUNNING,
                        ContextSummaryStageStatus.FAILED,
                    )
                ),
            )
            .values(
                status=ContextSummaryStageStatus.INVALIDATED,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if stage_result.rowcount != 1:
            await db.rollback()
            return False

        await db.execute(
            update(ContextSummaryFragment)
            .where(
                ContextSummaryFragment.work_dedupe_key == work_dedupe_key,
                ContextSummaryFragment.stage_key == stage_key,
                ContextSummaryFragment.model_key == model_key,
                ContextSummaryFragment.status == ContextSummaryFragmentStatus.COMPLETED,
            )
            .values(status=ContextSummaryFragmentStatus.INVALIDATED)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return True

    async def cleanup_by_work(
        self,
        db: AsyncSession,
        *,
        work_dedupe_key: str,
        batch_size: int = CONTEXT_SUMMARY_CLEANUP_BATCH_SIZE,
    ) -> int:
        _validate_batch_size(batch_size)
        fragment_ids = list((await db.execute(select(ContextSummaryFragment.id).where(ContextSummaryFragment.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryFragment.id).limit(batch_size))).scalars().all())
        if fragment_ids:
            result = await db.execute(delete(ContextSummaryFragment).where(ContextSummaryFragment.id.in_(fragment_ids)).execution_options(synchronize_session=False))
            await db.commit()
            return result.rowcount or 0

        stage_ids = list((await db.execute(select(ContextSummaryStage.id).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryStage.id).limit(batch_size))).scalars().all())
        if not stage_ids:
            return 0
        result = await db.execute(delete(ContextSummaryStage).where(ContextSummaryStage.id.in_(stage_ids)).execution_options(synchronize_session=False))
        await db.commit()
        return result.rowcount or 0

    async def cleanup_expired(
        self,
        db: AsyncSession,
        *,
        before: datetime,
        batch_size: int = CONTEXT_SUMMARY_CLEANUP_BATCH_SIZE,
    ) -> int:
        _validate_batch_size(batch_size)
        fragment_ids = list((await db.execute(select(ContextSummaryFragment.id).where(ContextSummaryFragment.created_at < before).order_by(ContextSummaryFragment.id).limit(batch_size))).scalars().all())
        if fragment_ids:
            result = await db.execute(delete(ContextSummaryFragment).where(ContextSummaryFragment.id.in_(fragment_ids)).execution_options(synchronize_session=False))
            await db.commit()
            return result.rowcount or 0

        fragment_exists = exists().where(
            ContextSummaryFragment.work_dedupe_key == ContextSummaryStage.work_dedupe_key,
            ContextSummaryFragment.stage_key == ContextSummaryStage.stage_key,
        )
        stage_ids = list(
            (
                await db.execute(
                    select(ContextSummaryStage.id)
                    .where(
                        ContextSummaryStage.created_at < before,
                        ~fragment_exists,
                    )
                    .order_by(ContextSummaryStage.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not stage_ids:
            return 0
        result = await db.execute(delete(ContextSummaryStage).where(ContextSummaryStage.id.in_(stage_ids)).execution_options(synchronize_session=False))
        await db.commit()
        return result.rowcount or 0


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


context_summary_stage_crud = CRUDContextSummaryStage(ContextSummaryStage)
context_summary_fragment_crud = CRUDContextSummaryFragment(ContextSummaryFragment)
