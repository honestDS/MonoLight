from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.crud.base import CRUDBase
from app.core.i18n import t
from app.core.i18n.locale import DEFAULT_LOCALE, normalize_locale
from app.models.system_setting import SystemRuntimeSettings, SystemSetting

DEFAULT_SYSTEM_SETTINGS = {
    "log_locale": DEFAULT_LOCALE,
    "temp_dir_max_size_mb": "1024",
    "audit_retention_days": "90",
    "audit_report_email": "",
    "session_reply_max_concurrency": "4",
}


class CRUDSystemSetting(CRUDBase[SystemSetting, SystemSetting, SystemSetting]):
    async def get_by_key(self, db: AsyncSession, key: str) -> SystemSetting | None:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        return result.scalars().first()

    async def _insert_if_missing(self, db: AsyncSession, *, key: str, value: str) -> SystemSetting:
        dialect_name = db.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = sqlite_insert(SystemSetting).values(key=key, value=value).on_conflict_do_nothing(index_elements=["key"])
        elif dialect_name == "mysql":
            statement = mysql_insert(SystemSetting).values(key=key, value=value).on_duplicate_key_update(value=SystemSetting.value)
        else:
            raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))
        await db.execute(statement)
        db_obj = await self.get_by_key(db, key)
        if db_obj is None:
            raise RuntimeError(f"Internal integrity error: system setting {key!r} was not found after insertion")
        return db_obj

    async def get_setup_status(self, db: AsyncSession) -> str | None:
        db_obj = await self.get_by_key(db, SETUP_STATUS_KEY)
        return db_obj.value if db_obj else None

    async def get_setup_admin_uid(self, db: AsyncSession) -> str | None:
        db_obj = await self.get_by_key(db, SETUP_ADMIN_UID_KEY)
        if db_obj is None or db_obj.value == "":
            return None
        return db_obj.value

    async def initialize_setup_state(self, db: AsyncSession, *, admin_uid: str | None) -> tuple[str, str | None]:
        initial_status = SETUP_STATUS_COMPLETED if admin_uid else SETUP_STATUS_PENDING
        await self._insert_if_missing(db, key=SETUP_STATUS_KEY, value=initial_status)
        await self._insert_if_missing(db, key=SETUP_ADMIN_UID_KEY, value=admin_uid or "")
        if admin_uid:
            await db.execute(update(SystemSetting).where(SystemSetting.key == SETUP_STATUS_KEY).values(value=SETUP_STATUS_COMPLETED).execution_options(synchronize_session=False))
            await db.execute(update(SystemSetting).where(SystemSetting.key == SETUP_ADMIN_UID_KEY, SystemSetting.value == "").values(value=admin_uid).execution_options(synchronize_session=False))
        status = await self.get_setup_status(db)
        if status is None:
            raise RuntimeError("Internal integrity error: setup status was not initialized")
        return status, await self.get_setup_admin_uid(db)

    async def claim_setup(self, db: AsyncSession) -> bool:
        result = await db.execute(update(SystemSetting).where(SystemSetting.key == SETUP_STATUS_KEY, SystemSetting.value == SETUP_STATUS_PENDING).values(value=SETUP_STATUS_CONFIGURING).execution_options(synchronize_session=False))
        return (result.rowcount or 0) == 1

    async def set_setup_admin_uid(self, db: AsyncSession, *, admin_uid: str) -> bool:
        result = await db.execute(update(SystemSetting).where(SystemSetting.key == SETUP_ADMIN_UID_KEY).values(value=admin_uid).execution_options(synchronize_session=False))
        return (result.rowcount or 0) == 1

    async def complete_setup(self, db: AsyncSession) -> bool:
        result = await db.execute(update(SystemSetting).where(SystemSetting.key == SETUP_STATUS_KEY, SystemSetting.value == SETUP_STATUS_CONFIGURING).values(value=SETUP_STATUS_COMPLETED).execution_options(synchronize_session=False))
        return (result.rowcount or 0) == 1

    async def get_runtime_settings(self, db: AsyncSession) -> SystemRuntimeSettings:
        result = await db.execute(select(SystemSetting))
        values = {item.key: item.value for item in result.scalars().all()}
        raw_log_locale = values.get("log_locale", DEFAULT_SYSTEM_SETTINGS["log_locale"])
        raw_temp_dir_max_size_mb = values.get("temp_dir_max_size_mb", DEFAULT_SYSTEM_SETTINGS["temp_dir_max_size_mb"])
        raw_audit_retention_days = values.get("audit_retention_days", DEFAULT_SYSTEM_SETTINGS["audit_retention_days"])
        raw_audit_report_email = values.get("audit_report_email", DEFAULT_SYSTEM_SETTINGS["audit_report_email"])
        raw_session_reply_max_concurrency = values.get("session_reply_max_concurrency", DEFAULT_SYSTEM_SETTINGS["session_reply_max_concurrency"])
        try:
            temp_dir_max_size_mb = int(raw_temp_dir_max_size_mb)
        except (TypeError, ValueError):
            temp_dir_max_size_mb = int(DEFAULT_SYSTEM_SETTINGS["temp_dir_max_size_mb"])
        try:
            audit_retention_days = int(raw_audit_retention_days)
        except (TypeError, ValueError):
            audit_retention_days = int(DEFAULT_SYSTEM_SETTINGS["audit_retention_days"])
        try:
            session_reply_max_concurrency = int(raw_session_reply_max_concurrency)
        except (TypeError, ValueError):
            session_reply_max_concurrency = int(DEFAULT_SYSTEM_SETTINGS["session_reply_max_concurrency"])
        return SystemRuntimeSettings(
            log_locale=normalize_locale(raw_log_locale),
            temp_dir_max_size_mb=temp_dir_max_size_mb,
            audit_retention_days=audit_retention_days,
            audit_report_email=str(raw_audit_report_email or "").strip(),
            session_reply_max_concurrency=session_reply_max_concurrency,
        )

    async def update_runtime_settings(self, db: AsyncSession, settings: SystemRuntimeSettings) -> SystemRuntimeSettings:
        normalized = settings.normalized()
        values = {
            "log_locale": normalized.log_locale,
            "temp_dir_max_size_mb": str(normalized.temp_dir_max_size_mb),
            "audit_retention_days": str(normalized.audit_retention_days),
            "audit_report_email": normalized.audit_report_email,
            "session_reply_max_concurrency": str(normalized.session_reply_max_concurrency),
        }
        for key, value in values.items():
            db_obj = await self.get_by_key(db, key)
            if db_obj:
                db_obj.value = value
                db.add(db_obj)
            else:
                db.add(SystemSetting(key=key, value=value))
        await db.commit()
        return await self.get_runtime_settings(db)

    async def ensure_defaults(self, db: AsyncSession) -> None:
        for key, value in DEFAULT_SYSTEM_SETTINGS.items():
            if not await self.get_by_key(db, key):
                db.add(SystemSetting(key=key, value=value))


system_setting_crud = CRUDSystemSetting(SystemSetting)
