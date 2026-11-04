import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t
from app.models.provider_usage import ProviderUsageDaily, ProviderUsageDetail
from app.models.system_setting import SystemSetting
from app.providers.database.client import ensure_sqlite_outer_transaction

_CLEANUP_SETTING_KEY = "provider_usage_detail_last_cleanup_utc"
_DETAIL_RETENTION = timedelta(hours=24)
_METRIC_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "image_count",
    "rerank_search_units",
)
_COUNTER_FIELDS = (
    "request_count",
    *_METRIC_FIELDS,
    *(f"{field}_reported_count" for field in _METRIC_FIELDS),
)
_DETAIL_COMPARE_FIELDS = tuple(column.name for column in ProviderUsageDetail.__table__.columns if column.name != "created_at")


def _unsupported_database(dialect_name: str) -> RuntimeError:
    return RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _bucket_key(detail: ProviderUsageDetail, usage_date) -> str:
    dimensions = [
        usage_date.isoformat(),
        detail.type,
        detail.channel_key,
        detail.channel_id,
        detail.channel_name,
        detail.model_id,
        detail.response_model_id,
        detail.protocol,
    ]
    serialized = json.dumps(dimensions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _details_match(existing: ProviderUsageDetail, detail: ProviderUsageDetail) -> bool:
    return all(getattr(existing, field) == getattr(detail, field) for field in _DETAIL_COMPARE_FIELDS)


def _daily_values(detail: ProviderUsageDetail, usage_date, bucket_key: str) -> dict[str, object]:
    values: dict[str, object] = {
        "bucket_key": bucket_key,
        "usage_date": usage_date,
        "type": detail.type,
        "channel_key": detail.channel_key,
        "channel_id": detail.channel_id,
        "channel_name": detail.channel_name,
        "model_id": detail.model_id,
        "response_model_id": detail.response_model_id,
        "protocol": detail.protocol,
        "request_count": 1,
    }
    for field in _METRIC_FIELDS:
        value = getattr(detail, field)
        values[field] = 0 if value is None else value
        values[f"{field}_reported_count"] = 0 if value is None else 1
    return values


def _daily_upsert(dialect_name: str, values: dict[str, object]):
    if dialect_name == "sqlite":
        statement = sqlite_insert(ProviderUsageDaily).values(**values)
        return statement.on_conflict_do_update(
            index_elements=[ProviderUsageDaily.bucket_key],
            set_=_counter_updates(statement.excluded),
        )

    if dialect_name == "mysql":
        statement = mysql_insert(ProviderUsageDaily).values(**values)
        return statement.on_duplicate_key_update(**_counter_updates(statement.inserted))

    raise _unsupported_database(dialect_name)


def _counter_updates(incoming) -> dict[str, object]:
    return {field: getattr(ProviderUsageDaily, field) + getattr(incoming, field) for field in _COUNTER_FIELDS}


def _cleanup_marker_insert(dialect_name: str):
    values = {"key": _CLEANUP_SETTING_KEY, "value": ""}
    if dialect_name == "sqlite":
        return sqlite_insert(SystemSetting).values(**values).on_conflict_do_nothing(index_elements=[SystemSetting.key])
    if dialect_name == "mysql":
        return mysql_insert(SystemSetting).values(**values).on_duplicate_key_update(value=SystemSetting.value)
    raise _unsupported_database(dialect_name)


class CRUDProviderUsage:
    async def create_once(self, db: AsyncSession, detail: ProviderUsageDetail) -> bool:
        dialect_name = db.get_bind().dialect.name
        if dialect_name not in {"sqlite", "mysql"}:
            raise _unsupported_database(dialect_name)

        await ensure_sqlite_outer_transaction(db)
        try:
            async with db.begin_nested():
                await db.execute(insert(ProviderUsageDetail.__table__).values(**detail.model_dump()))
        except IntegrityError:
            statement = select(ProviderUsageDetail).where(ProviderUsageDetail.provider_request_id == detail.provider_request_id).execution_options(populate_existing=True)
            if dialect_name == "mysql":
                statement = statement.with_for_update()
            result = await db.execute(statement)
            existing = result.scalars().first()
            if existing is None or not _details_match(existing, detail):
                raise
            return False

        usage_date = detail.created_at.date()
        values = _daily_values(detail, usage_date, _bucket_key(detail, usage_date))
        await db.execute(_daily_upsert(dialect_name, values))
        return True

    async def cleanup_if_due(self, db: AsyncSession, *, now: datetime) -> int:
        dialect_name = db.get_bind().dialect.name
        if dialect_name not in {"sqlite", "mysql"}:
            raise _unsupported_database(dialect_name)

        utc_now = _utc_naive(now)
        cleanup_before = utc_now - _DETAIL_RETENTION
        now_text = utc_now.isoformat(timespec="microseconds")
        cleanup_before_text = cleanup_before.isoformat(timespec="microseconds")
        claim_statement = (
            update(SystemSetting)
            .where(
                SystemSetting.key == _CLEANUP_SETTING_KEY,
                SystemSetting.value <= cleanup_before_text,
            )
            .values(value=now_text)
            .execution_options(synchronize_session=False)
        )

        claim = await db.execute(claim_statement)
        if (claim.rowcount or 0) != 1:
            marker = await db.execute(select(SystemSetting.key).where(SystemSetting.key == _CLEANUP_SETTING_KEY))
            if marker.scalar_one_or_none() is not None:
                return 0
            await db.execute(_cleanup_marker_insert(dialect_name))
            claim = await db.execute(claim_statement)
            if (claim.rowcount or 0) != 1:
                return 0

        result = await db.execute(delete(ProviderUsageDetail).where(ProviderUsageDetail.created_at < cleanup_before).execution_options(synchronize_session=False))
        return result.rowcount or 0


provider_usage_crud = CRUDProviderUsage()
