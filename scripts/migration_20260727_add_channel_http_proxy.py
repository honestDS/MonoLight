import json

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260727_add_channel_http_proxy"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str]:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return set()
        return {str(item["name"]) for item in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


def _parse_model_ids(value: object) -> list | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    return value


def _single_model_http_proxy(model_ids: list) -> str | None:
    proxies: set[str] = set()
    for model_item in model_ids:
        if not isinstance(model_item, dict):
            continue
        advanced_settings = model_item.get("advanced_settings")
        if not isinstance(advanced_settings, dict):
            continue
        http_proxy = advanced_settings.get("http_proxy")
        if isinstance(http_proxy, str) and http_proxy:
            proxies.add(http_proxy)

    if len(proxies) != 1:
        return None
    return next(iter(proxies))


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "channel")
    if not columns:
        return
    if "http_proxy" not in columns:
        await session.execute(text("ALTER TABLE channel ADD COLUMN http_proxy TEXT"))

    rows = (await session.execute(text("SELECT id, model_ids, http_proxy FROM channel"))).mappings()
    for row in rows:
        if row["http_proxy"] not in (None, ""):
            continue
        model_ids = _parse_model_ids(row["model_ids"])
        if model_ids is None:
            continue
        http_proxy = _single_model_http_proxy(model_ids)
        if http_proxy is None:
            continue
        await session.execute(
            text("UPDATE channel SET http_proxy = :http_proxy WHERE id = :channel_id AND (http_proxy IS NULL OR http_proxy = '')"),
            {"http_proxy": http_proxy, "channel_id": row["id"]},
        )
