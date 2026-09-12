from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

from .manager_common import (
    MemoryJobSubmissionResult,
    MemoryJobValidationError,
    _build_organization_retry_dedupe_key,
    _is_organization_retry_dedupe_key,
    _organization_interval_elapsed,
    _organization_retry_key_claims_stable_key,
    _validate_existing_organization_job,
    _validate_existing_organization_retry_job,
)

__all__ = [
    "MemoryJobOrganization",
]


class MemoryJobOrganization:
    async def _submit_organization_locked(
        self,
        db: AsyncSession,
        *,
        store: LongTermMemoryStore,
        uid: str,
        trigger: str,
        caller_dedupe_key: str | None,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.identifiers import build_memory_organization_active_mutation_key
        from app.core.memory.organization import (
            MemoryOrganizationContextExceededError,
            build_organization_dedupe_key,
            build_organization_execution_request,
            build_organization_job_payload,
            build_organization_snapshot,
            load_organization_model_config_for_store,
        )

        records = await memory_record_crud.list_for_organization(db, uid=uid)
        snapshot = build_organization_snapshot(
            records,
            active_embedding_revision=store.active_embedding_revision,
            index_revision=store.index_revision,
            policy_version=store.organization_policy_version,
        )
        final_dedupe_key = build_organization_dedupe_key(
            uid,
            snapshot_digest=snapshot.digest,
            policy_version=store.organization_policy_version,
            caller_dedupe_key=caller_dedupe_key,
        )
        active_mutation_key = build_memory_organization_active_mutation_key(uid)
        existing_job = await memory_job_crud.get_by_dedupe_key(
            db,
            uid=uid,
            dedupe_key=final_dedupe_key,
        )
        if existing_job is not None:
            existing_status = _validate_existing_organization_job(
                existing_job,
                uid=uid,
                dedupe_key=final_dedupe_key,
                snapshot_digest=snapshot.digest,
                policy_version=store.organization_policy_version,
                active_mutation_key=active_mutation_key,
            )
            if trigger != "auto" or existing_status not in {
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }:
                return MemoryJobSubmissionResult(job=existing_job, created=False)

            latest_job_id = store.organization_last_job_id
            if latest_job_id is not None and latest_job_id != existing_job.id:
                latest_job = await memory_job_crud.get_by_id(
                    db,
                    uid=uid,
                    job_id=latest_job_id,
                )
                if latest_job is not None and _organization_retry_key_claims_stable_key(
                    latest_job.dedupe_key,
                    stable_dedupe_key=final_dedupe_key,
                ):
                    if not _is_organization_retry_dedupe_key(
                        latest_job.dedupe_key,
                        stable_dedupe_key=final_dedupe_key,
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    latest_status = _validate_existing_organization_retry_job(
                        latest_job,
                        uid=uid,
                        stable_dedupe_key=final_dedupe_key,
                        snapshot_digest=snapshot.digest,
                        policy_version=store.organization_policy_version,
                        active_mutation_key=active_mutation_key,
                    )
                    if latest_status not in {
                        LongTermMemoryMutationStatus.FAILED,
                        LongTermMemoryMutationStatus.CANCELLED,
                    }:
                        return MemoryJobSubmissionResult(job=latest_job, created=False)
            final_dedupe_key = _build_organization_retry_dedupe_key(final_dedupe_key)

        organization_model = await load_organization_model_config_for_store(
            db,
            store=store,
            snapshot_count=snapshot.count,
        )
        payload = build_organization_job_payload(snapshot, organization_model, trigger=trigger)
        request = build_organization_execution_request(payload)
        if request.budget.exceeds_hard_window:
            raise MemoryOrganizationContextExceededError(request.budget)
        return await self.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key=final_dedupe_key,
            payload=payload,
            active_mutation_key=active_mutation_key,
            commit=False,
        )

    async def submit_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str | None = None,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.normalization import _normalize_dedupe_key, _normalize_uid, _validate_commit
        from app.core.memory.organization import validate_organization_submission_store

        normalized_commit = _validate_commit(commit)
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_dedupe_key = _normalize_dedupe_key(dedupe_key) if dedupe_key is not None else None
            store = await self._lock_organization_store(db, uid=normalized_uid)
            validate_organization_submission_store(store)
            submission = await self._submit_organization_locked(
                db,
                store=store,
                uid=normalized_uid,
                trigger="manual",
                caller_dedupe_key=normalized_dedupe_key,
            )
            if normalized_commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission
        except Exception:
            if normalized_commit:
                await db.rollback()
            raise

    async def submit_auto_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult | None:
        from app.core.memory.errors import MemoryConflictError
        from app.core.memory.identifiers import build_memory_organization_active_mutation_key
        from app.core.memory.normalization import _normalize_uid, _validate_commit
        from app.core.memory.organization import validate_organization_submission_store

        normalized_commit = _validate_commit(commit)

        async def skip() -> None:
            if normalized_commit:
                await db.commit()
            else:
                await db.flush()

        try:
            normalized_uid = _normalize_uid(uid)
            store = await self._lock_organization_store(db, uid=normalized_uid)
            if store.auto_organize_enabled is not True:
                await skip()
                return None
            try:
                validate_organization_submission_store(store)
            except MemoryConflictError as exc:
                if exc.message == ERR_MEMORY_MAINTENANCE_STATE_CONFLICT:
                    await skip()
                    return None
                raise

            active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
            if active_count < store.organize_trigger_records:
                await skip()
                return None

            now = await get_database_time(db)
            if not _organization_interval_elapsed(store.organization_last_run_at, now):
                await skip()
                return None

            active_job = await memory_job_crud.get_by_active_mutation_key(
                db,
                uid=normalized_uid,
                active_mutation_key=build_memory_organization_active_mutation_key(normalized_uid),
            )
            if active_job is not None:
                await skip()
                return None

            submission = await self._submit_organization_locked(
                db,
                store=store,
                uid=normalized_uid,
                trigger="auto",
                caller_dedupe_key=None,
            )
            if not submission.created:
                try:
                    submission_status = LongTermMemoryMutationStatus(submission.job.status)
                except (TypeError, ValueError) as exc:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
                if submission_status != LongTermMemoryMutationStatus.SUCCEEDED:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            job_id = submission.job.id
            if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            updated_store = await memory_store_crud.update_by_uid(
                db,
                uid=normalized_uid,
                organization_last_job_id=job_id,
                organization_last_run_at=now,
                organization_error=None,
                commit=False,
            )
            if updated_store is None:
                raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
            if normalized_commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission
        except Exception:
            if normalized_commit:
                await db.rollback()
            raise
