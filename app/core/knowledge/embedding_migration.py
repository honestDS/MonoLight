from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KB_EMBEDDING_CONFIG_CHANGED,
    ERR_KB_EMBEDDING_MIGRATION_NO_CHANGE,
    ERR_KB_EMBEDDING_PROBE_FAILED,
    ERR_KB_MANAGED_EMBEDDING_MIGRATION_FORBIDDEN,
    ERR_KB_NOT_FOUND,
    MANAGED_MEMORY_KB_MIGRATION_DEDUPE_PREFIX,
    MANAGED_MEMORY_KB_MIGRATION_MAX_ATTEMPTS,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.embedding.common import (
    build_embedding_signature,
    detect_embedding_dimensions,
    load_embedding_runtime_config,
)
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import ParameterException, ResourceNotFoundException
from app.core.knowledge_jobs.migration import prepare_knowledge_base_embedding_migration
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseMigrationStatus, KnowledgeBaseType, KnowledgeJob

_TERMINAL_RETRY_MIGRATION_STATUSES = {
    KnowledgeBaseMigrationStatus.FAILED,
    KnowledgeBaseMigrationStatus.CANCELLED,
}


async def _load_owned_knowledge_base(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
) -> KnowledgeBase:
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if knowledge_base is None or knowledge_base.uid != uid or knowledge_base.id is None:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    return knowledge_base


async def _load_migration_runtime_config(
    db: AsyncSession,
    channel_id: int,
    model_id: str,
    *,
    lock_for_reference_write: bool = False,
):
    try:
        return await load_embedding_runtime_config(
            db,
            channel_id,
            model_id,
            lock_for_reference_write=lock_for_reference_write,
        )
    except HTTPException as exc:
        message = exc.detail if isinstance(exc.detail, str) else ERR_KB_EMBEDDING_CONFIG_CHANGED
        raise ParameterException(message, code=exc.status_code) from exc


def _build_user_migration_dedupe_key(
    knowledge_base: KnowledgeBase,
    *,
    target_signature: str,
) -> str:
    base = f"user-kb-migration:{knowledge_base.id}:{knowledge_base.active_embedding_revision}:{target_signature}"
    if knowledge_base.migration_status in _TERMINAL_RETRY_MIGRATION_STATUSES and knowledge_base.migration_job_id is not None:
        return f"{base}:after:{knowledge_base.migration_job_id}"
    return base


async def submit_user_knowledge_base_embedding_migration(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    target_channel_id: int,
    target_model_id: str,
) -> KnowledgeJob:
    knowledge_base = await _load_owned_knowledge_base(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.USER:
        raise ParameterException(
            ERR_KB_MANAGED_EMBEDDING_MIGRATION_FORBIDDEN,
            code=409,
        )

    runtime_config = await _load_migration_runtime_config(
        db,
        target_channel_id,
        target_model_id,
    )
    await db.commit()
    try:
        target_dimensions = await detect_embedding_dimensions(runtime_config)
    except Exception as exc:
        raise ParameterException(ERR_KB_EMBEDDING_PROBE_FAILED, code=502) from exc

    try:
        current_runtime = await _load_migration_runtime_config(
            db,
            target_channel_id,
            target_model_id,
            lock_for_reference_write=True,
        )
        if current_runtime.declared_dimensions is not None and current_runtime.declared_dimensions != target_dimensions:
            raise ParameterException(ERR_KB_EMBEDDING_CONFIG_CHANGED, code=409)

        knowledge_base = await _load_owned_knowledge_base(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base.knowledge_base_type != KnowledgeBaseType.USER:
            raise ParameterException(
                ERR_KB_MANAGED_EMBEDDING_MIGRATION_FORBIDDEN,
                code=409,
            )

        target_signature = build_embedding_signature(
            target_channel_id,
            target_model_id,
            target_dimensions,
        )
        active = resolve_active_knowledge_base_embedding(knowledge_base)
        if active.channel_id == target_channel_id and active.model_id == target_model_id and active.dimensions == target_dimensions and knowledge_base.active_embedding_signature == target_signature:
            raise ParameterException(ERR_KB_EMBEDDING_MIGRATION_NO_CHANGE, code=409)

        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            target_channel_id=target_channel_id,
            target_model_id=target_model_id,
            target_dimensions=target_dimensions,
            target_signature=target_signature,
            dedupe_key=_build_user_migration_dedupe_key(
                knowledge_base,
                target_signature=target_signature,
            ),
            commit=False,
        )
        await db.commit()
        await db.refresh(job)
        return job
    except Exception:
        if db.in_transaction():
            await db.rollback()
        raise


async def submit_managed_knowledge_base_migrations_for_memory_revision(
    db: AsyncSession,
    *,
    uid: str,
    target_channel_id: int,
    target_model_id: str,
    target_dimensions: int,
    target_signature: str,
    memory_revision: int,
    commit: bool = True,
) -> list[KnowledgeJob]:
    jobs: list[KnowledgeJob] = []
    knowledge_bases = await knowledge_base_crud.lock_managed_by_uid(
        db,
        uid=uid,
    )
    try:
        for knowledge_base in knowledge_bases:
            if knowledge_base.id is None:
                raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
            active = resolve_active_knowledge_base_embedding(knowledge_base)
            if active.channel_id == target_channel_id and active.model_id == target_model_id and active.dimensions == target_dimensions and knowledge_base.active_embedding_signature == target_signature:
                continue
            job = await prepare_knowledge_base_embedding_migration(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base.id,
                target_channel_id=target_channel_id,
                target_model_id=target_model_id,
                target_dimensions=target_dimensions,
                target_signature=target_signature,
                dedupe_key=(f"{MANAGED_MEMORY_KB_MIGRATION_DEDUPE_PREFIX}:{knowledge_base.id}:{memory_revision}:{target_signature}"),
                max_attempts=MANAGED_MEMORY_KB_MIGRATION_MAX_ATTEMPTS,
                commit=False,
            )
            jobs.append(job)
        if commit:
            await db.commit()
            for job in jobs:
                await db.refresh(job)
        else:
            await db.flush()
        return jobs
    except Exception:
        if commit and db.in_transaction():
            await db.rollback()
        raise


__all__ = [
    "submit_managed_knowledge_base_migrations_for_memory_revision",
    "submit_user_knowledge_base_embedding_migration",
]
