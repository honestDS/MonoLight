import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from scripts import migration_20260918_add_message_model_context_suffix as model_context_suffix_migration
from scripts import migration_20260918_truncate_legacy_oversized_tool_results as oversized_tool_result_migration


@pytest.mark.asyncio
async def test_model_context_suffix_migration_adds_column_idempotently():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE message (
                        id INTEGER PRIMARY KEY,
                        session_id VARCHAR(100) NOT NULL,
                        uid VARCHAR(100) NOT NULL,
                        content TEXT
                    )
                    """
                )
            )

        async with session_factory() as session:
            await model_context_suffix_migration.migrate(session)
            await model_context_suffix_migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            columns = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("message")})
        assert "model_context_suffix" in columns
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_oversized_tool_result_migration_truncates_all_tool_results_and_invalidates_summary_once():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    oversized = "tool-output " * 40_000
    short = "short result"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE chat_session (
                        session_id VARCHAR(100) NOT NULL,
                        uid VARCHAR(100) NOT NULL,
                        context_summary TEXT,
                        context_summary_message_id INTEGER,
                        context_summary_revision INTEGER NOT NULL DEFAULT 0,
                        context_content_revision INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (session_id, uid)
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE message (
                        id INTEGER PRIMARY KEY,
                        session_id VARCHAR(100) NOT NULL,
                        uid VARCHAR(100) NOT NULL,
                        type VARCHAR(40) NOT NULL,
                        content TEXT,
                        content_revision INTEGER NOT NULL DEFAULT 0,
                        audit_record_id INTEGER
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO chat_session (
                        session_id,
                        uid,
                        context_summary,
                        context_summary_message_id,
                        context_summary_revision,
                        context_content_revision
                    ) VALUES (
                        'session-1',
                        'user-1',
                        'old summary',
                        9,
                        4,
                        7
                    )
                    """
                )
            )
            payloads = [
                (
                    1,
                    "TOOL_RESULT",
                    json.dumps(
                        {
                            "role": "tool",
                            "content": oversized,
                            "tool_call_id": "normal-call",
                        }
                    ),
                    None,
                ),
                (
                    2,
                    "tool_result",
                    json.dumps(
                        {
                            "role": "tool",
                            "content": oversized,
                            "tool_call_id": "audited-call",
                        }
                    ),
                    42,
                ),
                (
                    3,
                    "TOOL_RESULT",
                    json.dumps(
                        {
                            "role": "tool",
                            "content": short,
                            "tool_call_id": "short-call",
                        }
                    ),
                    None,
                ),
                (
                    4,
                    "TEXT",
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": oversized,
                        }
                    ),
                    None,
                ),
            ]
            for message_id, message_type, content, audit_record_id in payloads:
                await connection.execute(
                    text(
                        """
                        INSERT INTO message (
                            id,
                            session_id,
                            uid,
                            type,
                            content,
                            content_revision,
                            audit_record_id
                        ) VALUES (
                            :id,
                            'session-1',
                            'user-1',
                            :type,
                            :content,
                            0,
                            :audit_record_id
                        )
                        """
                    ),
                    {
                        "id": message_id,
                        "type": message_type,
                        "content": content,
                        "audit_record_id": audit_record_id,
                    },
                )

        async with session_factory() as session:
            await oversized_tool_result_migration.migrate(session)
            await session.commit()

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT id, content, content_revision
                        FROM message
                        ORDER BY id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            chat_session = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT
                            context_summary,
                            context_summary_message_id,
                            context_summary_revision,
                            context_content_revision
                        FROM chat_session
                        WHERE session_id = 'session-1'
                          AND uid = 'user-1'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )

        migrated_by_id = {int(row["id"]): row for row in rows}
        for message_id in (1, 2):
            payload = json.loads(migrated_by_id[message_id]["content"])
            assert oversized_tool_result_migration.TRUNCATION_NOTICE in payload["content"]
            assert migrated_by_id[message_id]["content_revision"] == 1
            encoding = oversized_tool_result_migration.tiktoken.get_encoding("cl100k_base")
            assert len(encoding.encode(payload["content"], disallowed_special=())) <= oversized_tool_result_migration.LEGACY_TOOL_RESULT_TOKEN_LIMIT

        assert json.loads(migrated_by_id[3]["content"])["content"] == short
        assert migrated_by_id[3]["content_revision"] == 0
        assert json.loads(migrated_by_id[4]["content"])["content"] == oversized
        assert migrated_by_id[4]["content_revision"] == 0
        assert chat_session["context_summary"] is None
        assert chat_session["context_summary_message_id"] is None
        assert chat_session["context_summary_revision"] == 5
        assert chat_session["context_content_revision"] == 8
    finally:
        await engine.dispose()
