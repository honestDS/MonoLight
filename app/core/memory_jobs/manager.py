import asyncio

from app.core.constants import LOG_MEMORY_AUTO_ORGANIZATION_SUBMISSION_FAILED
from app.core.crud.memory.job import MemoryJobCancelResult as MemoryJobCancelResult
from app.core.crud.memory.store import memory_store_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory_jobs.executor import SessionFactory

from .manager_cleanup import MemoryJobCleanup
from .manager_common import (
    MemoryJobSubmissionError as MemoryJobSubmissionError,
)
from .manager_common import (
    MemoryJobSubmissionResult as MemoryJobSubmissionResult,
)
from .manager_common import (
    MemoryJobTargetBusyError as MemoryJobTargetBusyError,
)
from .manager_common import (
    MemoryJobValidationError as MemoryJobValidationError,
)
from .manager_common import (
    _safe_auto_organization_error,
)
from .manager_common import (
    is_organization_chain_job as is_organization_chain_job,
)
from .manager_control import MemoryJobControl
from .manager_organization import MemoryJobOrganization
from .manager_organization_child import MemoryJobOrganizationChild
from .manager_query import MemoryJobQuery
from .manager_submission import MemoryJobSubmission

logger = get_logger(__name__)


class MemoryJobManager(
    MemoryJobSubmission,
    MemoryJobOrganization,
    MemoryJobCleanup,
    MemoryJobOrganizationChild,
    MemoryJobQuery,
    MemoryJobControl,
):
    pass


memory_job_manager = MemoryJobManager()


async def best_effort_submit_auto_organization_after_publication(
    session_factory: SessionFactory,
    uid: str,
    source_job_id: int,
) -> None:
    try:
        async with session_factory() as db:
            await memory_job_manager.submit_auto_organization(db, uid=uid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_key, error_text = _safe_auto_organization_error(exc)
        logger.bind(
            uid=uid,
            source_job_id=source_job_id,
            exception_type=type(exc).__name__,
            error_key=error_key,
            error_text=error_text,
        ).warning(t(LOG_MEMORY_AUTO_ORGANIZATION_SUBMISSION_FAILED))
        try:
            async with session_factory() as db:
                store = await memory_store_crud.lock_for_mutation(
                    db,
                    uid=uid,
                    commit=False,
                )
                if store is None:
                    return
                updated_store = await memory_store_crud.update_by_uid(
                    db,
                    uid=uid,
                    organization_error=error_text,
                    commit=False,
                )
                if updated_store is None:
                    return
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as update_exc:
            logger.bind(
                uid=uid,
                source_job_id=source_job_id,
                exception_type=type(update_exc).__name__,
                error_key=error_key,
                error_text=error_text,
            ).error(t("LOG_MEMORY_JOB_DATABASE_OPERATION_FAILED"))


__all__ = [
    "MemoryJobCancelResult",
    "MemoryJobManager",
    "MemoryJobSubmissionError",
    "MemoryJobSubmissionResult",
    "MemoryJobTargetBusyError",
    "MemoryJobValidationError",
    "best_effort_submit_auto_organization_after_publication",
    "is_organization_chain_job",
    "memory_job_manager",
]
