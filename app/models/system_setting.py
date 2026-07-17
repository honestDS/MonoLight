from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from app.core.i18n.locale import DEFAULT_LOCALE, normalize_locale


class SystemSettingBase(SQLModel):
    key: str = Field(index=True, unique=True, nullable=False, max_length=100)
    value: str = Field(nullable=False, max_length=1000)


class SystemSetting(SystemSettingBase, table=True):
    __tablename__ = "system_setting"

    id: int | None = Field(default=None, primary_key=True, index=True)


class SystemRuntimeSettings(SQLModel):
    log_locale: str = Field(default=DEFAULT_LOCALE)
    temp_dir_max_size_mb: int = Field(default=1024, ge=1, le=1048576)
    audit_retention_days: int = Field(default=90, ge=1, le=3650)
    audit_report_email: str = Field(default="", max_length=320)
    session_reply_max_concurrency: int = Field(default=4, ge=1, le=100)

    model_config = ConfigDict(validate_assignment=True)

    def normalized(self) -> "SystemRuntimeSettings":
        self.log_locale = normalize_locale(self.log_locale)
        self.audit_report_email = self.audit_report_email.strip()
        return self
