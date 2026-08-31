import asyncio
from datetime import timedelta

from app.core.constants import ERR_VALUE_MUST_BE_POSITIVE
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.time import get_local_time
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

CONTEXT_SUMMARY_STAGE_RETENTION_HOURS = 24
CONTEXT_SUMMARY_CLEANUP_INTERVAL_SECONDS = 3600


async def cleanup_context_summary_work(work_dedupe_key: str) -> int:
    deleted_count = 0
    while True:
        async with AsyncSessionLocal() as db:
            batch_count = await context_summary_stage_crud.cleanup_by_work(
                db,
                work_dedupe_key=work_dedupe_key,
            )
        if batch_count == 0:
            return deleted_count
        deleted_count += batch_count
        await asyncio.sleep(0)


async def cleanup_context_summary_work_safely(work_dedupe_key: str) -> None:
    try:
        await cleanup_context_summary_work(work_dedupe_key)
    except Exception:
        logger.bind(work_dedupe_key=work_dedupe_key).exception(
            "Context summary work cleanup failed",
        )


async def cleanup_expired_context_summary_stages(
    *,
    retention_hours: int = CONTEXT_SUMMARY_STAGE_RETENTION_HOURS,
) -> int:
    if retention_hours < 1:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="retention_hours"))

    before = get_local_time() - timedelta(hours=retention_hours)
    deleted_count = 0
    while True:
        async with AsyncSessionLocal() as db:
            batch_count = await context_summary_stage_crud.cleanup_expired(
                db,
                before=before,
            )
        if batch_count == 0:
            return deleted_count
        deleted_count += batch_count
        await asyncio.sleep(0)


async def background_context_summary_cleaner(
    interval_seconds: int = CONTEXT_SUMMARY_CLEANUP_INTERVAL_SECONDS,
    retention_hours: int = CONTEXT_SUMMARY_STAGE_RETENTION_HOURS,
) -> None:
    if interval_seconds < 1:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="interval_seconds"))

    while True:
        try:
            deleted_count = await cleanup_expired_context_summary_stages(
                retention_hours=retention_hours,
            )
            if deleted_count > 0:
                logger.bind(
                    deleted_count=deleted_count,
                    retention_hours=retention_hours,
                ).info("Expired context summary stage records cleaned")
        except Exception:
            logger.exception("Context summary stage cleanup failed")

        await asyncio.sleep(interval_seconds)
