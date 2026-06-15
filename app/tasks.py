import asyncio

from app.core import constants
from app.core.crud.log import system_log_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


async def background_log_cleaner(days: int = 7):
    while True:
        try:
            async with AsyncSessionLocal() as db:
                deleted_count = await system_log_crud.clear_expired_logs(db, days=days)
                await db.commit()
                if deleted_count > 0:
                    logger.bind(deleted_count=deleted_count, retention_days=days).info(t(constants.MSG_LOG_CLEANER_CLEARED, deleted_count=deleted_count))
        except Exception as e:
            logger.bind(retention_days=days).error(t(constants.ERR_LOG_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(86400)  # 24 hours
