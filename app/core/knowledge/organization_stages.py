from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE
from app.core.crud.knowledge.organization import (
    knowledge_organization_fragment_crud,
    knowledge_organization_stage_crud,
)
from app.core.i18n import t
from app.core.knowledge.organization_runtime import KnowledgeOrganizationModelConfig
from app.core.knowledge.organization_scope import _compact_scope_descriptor, _scope_descriptor
from app.core.knowledge.organization_types import KnowledgeOrganizationFragmentResult
from app.models.knowledge_base import (
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
)

__all__ = []


def _model_key(model: KnowledgeOrganizationModelConfig) -> str:
    execution_model = {
        "channel_id": model.channel_id,
        "model_id": model.model_id,
        "protocol": model.protocol,
    }
    return hashlib.sha256(canonical_json_dumps(execution_model).encode("utf-8")).hexdigest()


def _stage_key(
    *,
    snapshot_key: str,
    work_key: str,
    stage_index: int,
    lower_stage_key: str | None,
    model_key: str,
    expected_fragment_count: int,
    purpose: str,
) -> str:
    payload = {
        "snapshot_key": snapshot_key,
        "work_key": work_key,
        "stage_index": stage_index,
        "lower_stage_key": lower_stage_key,
        "model_key": model_key,
        "expected_fragment_count": expected_fragment_count,
        "purpose": purpose,
    }
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


def _stage_model_snapshot(model: KnowledgeOrganizationModelConfig, *, purpose: str) -> dict[str, Any]:
    return {
        "execution_model": {
            "channel_id": model.channel_id,
            "model_id": model.model_id,
            "protocol": model.protocol,
        },
        "purpose": purpose,
    }


async def _write_plan_fragment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
    result: KnowledgeOrganizationFragmentResult,
) -> None:
    fragment = KnowledgeOrganizationFragment(
        dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            fragment_index=result.fragment_index,
        ),
        uid=stage.uid,
        knowledge_base_id=stage.knowledge_base_id,
        snapshot_id=stage.snapshot_id,
        stage_id=stage.id,
        work_key=stage.work_key,
        snapshot_key=stage.snapshot_key,
        stage_key=stage.stage_key,
        model_key=stage.model_key,
        fragment_index=result.fragment_index,
        candidate_scope={
            "items": [_scope_descriptor(item) for item in result.scope],
            "output_items": [_compact_scope_descriptor(item) for item in result.output_scope],
            "input_tokens": result.input_tokens,
        },
        result=result.plan.model_dump(mode="json"),
        status=KnowledgeOrganizationFragmentStatus.COMPLETED,
    )
    async with session_factory() as db:
        persisted, _created = await knowledge_organization_fragment_crud.write_ordered(db, fragment=fragment)
    if persisted is None or persisted.candidate_scope != fragment.candidate_scope or persisted.result != fragment.result:
        raise RuntimeError(t("organization fragment persistence conflict"))


async def _stage_resume_index(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> int:
    async with session_factory() as db:
        value = await knowledge_organization_stage_crud.get_resume_fragment_index(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
    if value is None:
        raise RuntimeError(t("organization stage cannot resume"))
    return value


async def _mark_stage_completed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> None:
    async with session_factory() as db:
        completed = await knowledge_organization_stage_crud.mark_completed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
    if not completed:
        async with session_factory() as db:
            current = await knowledge_organization_stage_crud.get_by_identity(db, work_key=stage.work_key, stage_key=stage.stage_key)
        if current is None or current.status != KnowledgeOrganizationStageStatus.COMPLETED:
            raise RuntimeError(t("organization stage completion barrier rejected"))


async def _fail_and_invalidate_stage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
    error: str,
) -> None:
    async with session_factory() as db:
        await knowledge_organization_stage_crud.mark_failed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
            error=error,
        )
    async with session_factory() as db:
        await knowledge_organization_stage_crud.invalidate(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )


async def _create_stage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    stage_index: int,
    lower_stage_key: str | None,
    model: KnowledgeOrganizationModelConfig,
    expected_fragment_count: int,
    purpose: str,
) -> KnowledgeOrganizationStage:
    model_key = _model_key(model)
    stage_key = _stage_key(
        snapshot_key=snapshot.snapshot_key,
        work_key=work_key,
        stage_index=stage_index,
        lower_stage_key=lower_stage_key,
        model_key=model_key,
        expected_fragment_count=expected_fragment_count,
        purpose=purpose,
    )
    stage = KnowledgeOrganizationStage(
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        snapshot_id=snapshot.id,
        work_key=work_key,
        snapshot_key=snapshot.snapshot_key,
        stage_key=stage_key,
        stage_index=stage_index,
        lower_stage_key=lower_stage_key,
        model_key=model_key,
        model_snapshot=_stage_model_snapshot(model, purpose=purpose),
        expected_fragment_count=expected_fragment_count,
    )
    async with session_factory() as db:
        persisted, _created = await knowledge_organization_stage_crud.create_stage(db, stage=stage)
    return persisted


async def _iter_stage_fragments(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> AsyncIterator[KnowledgeOrganizationFragment]:
    after_index = -1
    expected_index = 0
    while True:
        async with session_factory() as db:
            page = await knowledge_organization_fragment_crud.list_stage_page(
                db,
                stage_id=stage.id,
                after_fragment_index=after_index,
                limit=KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
            )
        if not page:
            break
        for fragment in page:
            if fragment.fragment_index != expected_index or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED:
                raise RuntimeError(t("organization stage fragment sequence invalid"))
            yield fragment
            expected_index += 1
            after_index = fragment.fragment_index
        if len(page) < KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE:
            break
    if expected_index != stage.expected_fragment_count:
        raise RuntimeError(t("organization completed stage fragment count invalid"))
