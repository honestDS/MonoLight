from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.i18n.locale import DEFAULT_LOCALE, normalize_locale
from app.models.system_setting import SystemRuntimeSettings, SystemSetting

DEFAULT_SYSTEM_SETTINGS = {
    "log_locale": DEFAULT_LOCALE,
    "temp_dir_max_size_mb": "1024",
}


class CRUDSystemSetting(CRUDBase[SystemSetting, SystemSetting, SystemSetting]):
    async def get_by_key(self, db: AsyncSession, key: str) -> SystemSetting | None:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        return result.scalars().first()

    async def get_runtime_settings(self, db: AsyncSession) -> SystemRuntimeSettings:
        result = await db.execute(select(SystemSetting))
        values = {item.key: item.value for item in result.scalars().all()}
        raw_log_locale = values.get("log_locale", DEFAULT_SYSTEM_SETTINGS["log_locale"])
        raw_temp_dir_max_size_mb = values.get("temp_dir_max_size_mb", DEFAULT_SYSTEM_SETTINGS["temp_dir_max_size_mb"])
        try:
            temp_dir_max_size_mb = int(raw_temp_dir_max_size_mb)
        except (TypeError, ValueError):
            temp_dir_max_size_mb = int(DEFAULT_SYSTEM_SETTINGS["temp_dir_max_size_mb"])
        return SystemRuntimeSettings(log_locale=normalize_locale(raw_log_locale), temp_dir_max_size_mb=temp_dir_max_size_mb)

    async def update_runtime_settings(self, db: AsyncSession, settings: SystemRuntimeSettings) -> SystemRuntimeSettings:
        normalized = settings.normalized()
        values = {
            "log_locale": normalized.log_locale,
            "temp_dir_max_size_mb": str(normalized.temp_dir_max_size_mb),
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
