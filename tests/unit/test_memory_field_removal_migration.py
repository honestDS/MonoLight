from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.memory import LongTermMemoryRecord, LongTermMemoryRevision
from scripts import migration_20260803_add_longterm_memory as add_memory_migration
from scripts import migration_20260805_remove_memory_importance_scope as remove_fields_migration


def _decode_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_memory_field_removal_migration_cleans_legacy_json_and_drops_columns(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-field-removal.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await add_memory_migration.migrate(session)
            await session.execute(
                text("INSERT INTO long_term_memory_record (uid, memory_key, memory_type, importance, scope, content) VALUES (:uid, :memory_key, :memory_type, :importance, :scope, :content)"),
                {
                    "uid": "legacy-user",
                    "memory_key": "legacy-key",
                    "memory_type": "fact",
                    "importance": 8,
                    "scope": "legacy-scope",
                    "content": "legacy content",
                },
            )
            await session.execute(
                text("INSERT INTO long_term_memory_revision (uid, memory_id, version, memory_key, memory_type, importance, scope, content) VALUES (:uid, :memory_id, :version, :memory_key, :memory_type, :importance, :scope, :content)"),
                {
                    "uid": "legacy-user",
                    "memory_id": 1,
                    "version": 1,
                    "memory_key": "legacy-key",
                    "memory_type": "fact",
                    "importance": 8,
                    "scope": "legacy-scope",
                    "content": "legacy content",
                },
            )
            payload = {
                "importance": 8,
                "scope": "legacy-scope",
                "nested": {"importance": 4, "scope": "nested-scope", "keep": True},
                "items": [{"importance": 2, "scope": "item-scope", "keep": "value"}],
            }
            result = {"record_snapshot": {"importance": 8, "scope": "legacy-scope", "version": 1}}
            await session.execute(
                text("INSERT INTO long_term_memory_mutation_job (uid, operation, dedupe_key, payload, result) VALUES (:uid, :operation, :dedupe_key, :payload, :result)"),
                {
                    "uid": "legacy-user",
                    "operation": "create",
                    "dedupe_key": "legacy-job",
                    "payload": json.dumps(payload),
                    "result": json.dumps(result),
                },
            )
            await session.execute(
                text("INSERT INTO long_term_memory_embedding_delta (uid, migration_job_id, sequence, snapshot) VALUES (:uid, :migration_job_id, :sequence, :snapshot)"),
                {
                    "uid": "legacy-user",
                    "migration_job_id": 1,
                    "sequence": 1,
                    "snapshot": json.dumps({"importance": 6, "scope": "delta-scope", "version": 1}),
                },
            )
            await session.commit()

            await remove_fields_migration.migrate(session)
            await session.commit()
            await remove_fields_migration.migrate(session)
            await session.commit()

            job_row = (await session.execute(text("SELECT payload, result FROM long_term_memory_mutation_job WHERE dedupe_key = 'legacy-job'"))).mappings().one()
            delta_snapshot = (await session.execute(text("SELECT snapshot FROM long_term_memory_embedding_delta WHERE sequence = 1"))).scalar_one()
            record_content = (await session.execute(text("SELECT content FROM long_term_memory_record WHERE id = 1"))).scalar_one()
            revision_content = (await session.execute(text("SELECT content FROM long_term_memory_revision WHERE memory_id = 1"))).scalar_one()

        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "record_columns": {item["name"] for item in inspect(sync_connection).get_columns("long_term_memory_record")},
                    "revision_columns": {item["name"] for item in inspect(sync_connection).get_columns("long_term_memory_revision")},
                    "record_indexes": {item["name"] for item in inspect(sync_connection).get_indexes("long_term_memory_record")},
                    "revision_indexes": {item["name"] for item in inspect(sync_connection).get_indexes("long_term_memory_revision")},
                }
            )

        cleaned_payload = _decode_json(job_row["payload"])
        cleaned_result = _decode_json(job_row["result"])
        cleaned_delta = _decode_json(delta_snapshot)
        assert cleaned_payload == {"nested": {"keep": True}, "items": [{"keep": "value"}]}
        assert cleaned_result == {"record_snapshot": {"version": 1}}
        assert cleaned_delta == {"version": 1}
        assert record_content == "legacy content"
        assert revision_content == "legacy content"
        assert {"importance", "scope"}.isdisjoint(schema["record_columns"])
        assert {"importance", "scope"}.isdisjoint(schema["revision_columns"])
        assert "ix_long_term_memory_record_importance" not in schema["record_indexes"]
        assert "ix_long_term_memory_record_scope" not in schema["record_indexes"]
        assert "ix_long_term_memory_revision_scope" not in schema["revision_indexes"]
        assert {"importance", "scope"}.isdisjoint(LongTermMemoryRecord.__table__.c.keys())
        assert {"importance", "scope"}.isdisjoint(LongTermMemoryRevision.__table__.c.keys())
    finally:
        await engine.dispose()
