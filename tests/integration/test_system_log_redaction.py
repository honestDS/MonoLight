import json
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1 import system
from app.core.constants import LOG_REDACTION_PLACEHOLDER
from app.core.crud.system.log import system_log_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.log import LogManager
from app.core.tools import (
    KNOWLEDGE_BASE_QUERY_TOOL_NAME,
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
    TOOL_EXECUTOR_MAP,
)
from app.core.tools.base import BaseExecutor
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.core.utils.time import get_local_time
from app.handler import register_handlers
from app.models.message import InternalToolCall
from app.models.profile import Profile, ProfileConfig
from app.models.system_log import SystemLog, SystemLogCreate
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "system-log-redaction-integration-jwt-secret"
SETTINGS_PATH = "/api/v1/system/settings"
LOGS_PATH = "/api/v1/system/logs"
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


@pytest_asyncio.fixture
async def log_session_factory(
    database_factory,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[User.__table__, SystemSetting.__table__, SystemLog.__table__],
        name="system-log-redaction.sqlite3",
    ) as session_factory:
        async with session_factory() as session:
            session.add(User(uid="admin", username="admin", is_active=True, is_superuser=True))
            await session.commit()
        yield session_factory


@pytest_asyncio.fixture
async def api_app(
    log_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", log_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(system.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with log_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


def _token() -> str:
    return security.create_access_token(data={"sub": "admin"})


def _json_payload(tag: str) -> str:
    return json.dumps(
        {
            "event": tag,
            "nested": {
                "credentials": {"token": f"{tag}-credential"},
                "content": f"{tag}-content",
                "query": f"{tag}-query",
                "error": f"{tag}-error",
                "source": f"{tag}-source",
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def _assert_payload_redacted(value: str) -> None:
    nested = json.loads(value)["nested"]
    assert nested["credentials"] == LOG_REDACTION_PLACEHOLDER
    assert nested["content"] == LOG_REDACTION_PLACEHOLDER
    assert nested["query"] == LOG_REDACTION_PLACEHOLDER
    assert nested["error"] == LOG_REDACTION_PLACEHOLDER
    assert nested["source"] == LOG_REDACTION_PLACEHOLDER


async def _create_log(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    module: str,
    message: str,
    extra: str,
) -> SystemLog:
    async with session_factory() as session:
        return await system_log_crud.create(
            session,
            obj_in=SystemLogCreate(
                level="INFO",
                module=module,
                message=message,
                extra=extra,
                uid="log-user",
                session_id="log-session",
                created_at=get_local_time(),
            ),
        )


async def _snapshot_logs(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, dict[str, Any]]:
    async with session_factory() as session:
        result = await session.execute(select(SystemLog).order_by(SystemLog.id))
        return {
            log.module: {
                "id": log.id,
                "level": log.level,
                "module": log.module,
                "message": log.message,
                "uid": log.uid,
                "session_id": log.session_id,
                "extra": log.extra,
                "created_at": log.created_at,
            }
            for log in result.scalars().all()
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("level", LOG_LEVELS)
@pytest.mark.parametrize("enabled", (False, True), ids=("redaction-off", "redaction-on"))
async def test_create_preserves_all_levels_and_does_not_mutate_inputs(
    log_session_factory: async_sessionmaker[AsyncSession],
    level: str,
    enabled: bool,
) -> None:
    raw_message = _json_payload(f"{level}-message")
    raw_extra = _json_payload(f"{level}-extra")

    async with log_session_factory() as session:
        if enabled:
            session.add(SystemSetting(key="log_redaction_enabled", value="true"))
            await session.commit()

        dict_input = {
            "level": level,
            "module": "dict-module-before",
            "message": raw_message,
            "uid": "dict-user-before",
            "session_id": "dict-session-before",
            "extra": raw_extra,
            "created_at": get_local_time(),
        }
        dict_input_before = dict(dict_input)
        update_dict = {
            "module": "dict-module-after",
            "uid": "dict-user-after",
            "session_id": "dict-session-after",
        }
        update_dict_before = dict(update_dict)
        dict_log = await system_log_crud.create(
            session,
            obj_in=dict_input,
            update_dict=update_dict,
        )

        object_input = SystemLogCreate(
            level=level,
            module="object-module",
            message=raw_message,
            uid="object-user",
            session_id="object-session",
            extra=raw_extra,
            created_at=get_local_time(),
        )
        object_input_before = object_input.model_dump()
        object_log = await system_log_crud.create(session, obj_in=object_input)

    assert dict_input == dict_input_before
    assert update_dict == update_dict_before
    assert object_input.model_dump() == object_input_before

    assert (dict_log.level, dict_log.module, dict_log.uid, dict_log.session_id) == (
        level,
        "dict-module-after",
        "dict-user-after",
        "dict-session-after",
    )
    assert (object_log.level, object_log.module, object_log.uid, object_log.session_id) == (
        level,
        "object-module",
        "object-user",
        "object-session",
    )

    for log in (dict_log, object_log):
        if enabled:
            _assert_payload_redacted(log.message)
            _assert_payload_redacted(log.extra)
        else:
            assert log.message == raw_message
            assert log.extra == raw_extra


@pytest.mark.asyncio
async def test_create_with_commit_false_can_be_rolled_back(
    log_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_message = _json_payload("rollback-message")
    raw_extra = _json_payload("rollback-extra")

    async with log_session_factory() as session:
        session.add(SystemSetting(key="log_redaction_enabled", value="true"))
        await session.commit()
        pending = await system_log_crud.create(
            session,
            obj_in=SystemLogCreate(
                level="DEBUG",
                module="rollback",
                message=raw_message,
                uid="rollback-user",
                session_id="rollback-session",
                extra=raw_extra,
                created_at=get_local_time(),
            ),
            commit=False,
        )
        assert pending.message != raw_message
        assert len(await system_log_crud.get_multi_filtered(session)) == 1
        await session.rollback()

    async with log_session_factory() as session:
        assert await system_log_crud.get_multi_filtered(session) == []


@pytest.mark.asyncio
async def test_admin_toggle_redacts_only_new_logs_and_get_returns_stored_values(
    api_app: FastAPI,
    log_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = {"Authorization": f"Bearer {_token()}"}
    raw_message = _json_payload("switch-message")
    raw_extra = _json_payload("switch-extra")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        settings = await client.get(SETTINGS_PATH, headers=headers)
        assert settings.status_code == 200
        assert settings.json()["data"]["log_redaction_enabled"] is False

        await _create_log(
            log_session_factory,
            module="before-enable",
            message=raw_message,
            extra=raw_extra,
        )
        before_snapshot = await _snapshot_logs(log_session_factory)
        assert before_snapshot["before-enable"]["message"] == raw_message
        assert before_snapshot["before-enable"]["extra"] == raw_extra

        enabled = await client.post(SETTINGS_PATH, headers=headers, json={"log_redaction_enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["data"]["log_redaction_enabled"] is True

        await _create_log(
            log_session_factory,
            module="while-enabled",
            message=raw_message,
            extra=raw_extra,
        )
        after_enable_snapshot = await _snapshot_logs(log_session_factory)
        assert after_enable_snapshot["before-enable"] == before_snapshot["before-enable"]
        _assert_payload_redacted(after_enable_snapshot["while-enabled"]["message"])
        _assert_payload_redacted(after_enable_snapshot["while-enabled"]["extra"])

        enabled_logs = await client.get(LOGS_PATH, headers=headers)
        assert enabled_logs.status_code == 200
        enabled_by_module = {item["module"]: item for item in enabled_logs.json()["data"]["items"]}
        assert enabled_by_module["before-enable"]["message"] == raw_message
        assert enabled_by_module["before-enable"]["extra"] == raw_extra
        assert enabled_by_module["while-enabled"]["message"] == after_enable_snapshot["while-enabled"]["message"]
        assert enabled_by_module["while-enabled"]["extra"] == after_enable_snapshot["while-enabled"]["extra"]

        disabled = await client.post(SETTINGS_PATH, headers=headers, json={"log_redaction_enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["data"]["log_redaction_enabled"] is False

        await _create_log(
            log_session_factory,
            module="after-disable",
            message=raw_message,
            extra=raw_extra,
        )
        final_snapshot = await _snapshot_logs(log_session_factory)
        assert final_snapshot["before-enable"] == before_snapshot["before-enable"]
        assert final_snapshot["while-enabled"] == after_enable_snapshot["while-enabled"]
        assert final_snapshot["after-disable"]["message"] == raw_message
        assert final_snapshot["after-disable"]["extra"] == raw_extra

        final_logs = await client.get(LOGS_PATH, headers=headers)

    assert final_logs.status_code == 200
    final_by_module = {item["module"]: item for item in final_logs.json()["data"]["items"]}
    assert set(final_by_module) == set(final_snapshot)
    for module, stored in final_snapshot.items():
        assert final_by_module[module]["message"] == stored["message"]
        assert final_by_module[module]["extra"] == stored["extra"]
        assert final_by_module[module]["uid"] == stored["uid"]
        assert final_by_module[module]["session_id"] == stored["session_id"]


TOOL_RESULT = {
    "items": [{"content": "result-items-content", "source": "result-items-source"}],
    "chat_history": [{"content": "result-chat-history-content"}],
    "knowledge_base": [{"content": "result-knowledge-base-content", "source": "result-knowledge-base-source"}],
    "error": "result-error-body",
}
TOOL_RESULT_JSON = json.dumps(TOOL_RESULT, ensure_ascii=False)
TOOL_RESULT_SECRETS = [
    "result-items-content",
    "result-items-source",
    "result-chat-history-content",
    "result-knowledge-base-content",
    "result-knowledge-base-source",
    "result-error-body",
]
TOOL_CASES = (
    {
        "id": "manage-memory-and-knowledge",
        "name": MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
        "memory_enabled": True,
        "arguments": {
            "operation": "recall",
            "query": "call-query-secret",
            "knowledge_query": "call-knowledge-query-secret",
            "content": "call-content-secret",
            "knowledge_content": "call-knowledge-content-secret",
            "change_evidence": "call-change-evidence-secret",
        },
        "metadata": {"operation": "recall"},
    },
    {
        "id": "query-knowledge-base",
        "name": KNOWLEDGE_BASE_QUERY_TOOL_NAME,
        "memory_enabled": False,
        "arguments": {"knowledge_base_id": 17, "query": "call-query-secret"},
        "metadata": {"knowledge_base_id": 17},
    },
)


class FixedResultExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, **kwargs: Any) -> str:
        return TOOL_RESULT_JSON


def _system_log_from_record(record: dict[str, Any]) -> SystemLogCreate:
    record_extra = record["extra"]
    extra = {key: value for key, value in record_extra.items() if key not in {"name", "uid", "session_id"}}
    return SystemLogCreate(
        level=record["level"].name,
        module=record_extra.get("name") or record["name"],
        message=record["message"],
        uid=record_extra.get("uid"),
        session_id=record_extra.get("session_id"),
        extra=json.dumps(extra, default=str) if extra else None,
        created_at=get_local_time(),
    )


def _tool_log_payload(message: str) -> dict[str, Any]:
    return json.loads(message[message.index("{") :])


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_case", TOOL_CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("enabled", (False, True), ids=("redaction-off", "redaction-on"))
async def test_process_single_tool_logs_original_data_and_crud_applies_setting(
    log_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tool_case: dict[str, Any],
    enabled: bool,
) -> None:
    tool_name = tool_case["name"]
    monkeypatch.setitem(TOOL_EXECUTOR_MAP, tool_name, FixedResultExecutor)
    cfg = ProfileConfig.model_validate(
        {
            "channel": {},
            "security": {},
            "tool": {},
            "other": {},
            "memory": {"enabled": tool_case["memory_enabled"]},
        }
    )
    profile = Profile(uid="tool-user", name="tool-profile", configs={})
    call = InternalToolCall(
        id="redaction-test-call",
        name=tool_name,
        arguments=dict(tool_case["arguments"]),
    )
    captured: list[dict[str, Any]] = []

    def capture(message: Any) -> None:
        record = message.record
        if "tool_call" in record["extra"] or "tool_result" in record["extra"]:
            captured.append(
                {
                    "level": record["level"],
                    "name": record["name"],
                    "message": record["message"],
                    "extra": dict(record["extra"]),
                }
            )

    sink_id = logger.add(capture, level="DEBUG", enqueue=False)
    try:
        async with log_session_factory() as session:
            if enabled:
                session.add(SystemSetting(key="log_redaction_enabled", value="true"))
                await session.commit()

            result = await process_single_tool(
                call,
                session,
                profile,
                cfg,
                messages=[],
                username="tool-user",
                session_id="tool-session",
                turn=1,
                uid="tool-user",
                allowed_knowledge_base_ids=[17],
            )
            assert result.content == TOOL_RESULT_JSON
            assert len(captured) == 2

            call_record = next(record for record in captured if "tool_call" in record["extra"])
            result_record = next(record for record in captured if "tool_result" in record["extra"])
            call_secrets = [tool_case["arguments"][key] for key in ("query", "knowledge_query", "content", "knowledge_content", "change_evidence") if key in tool_case["arguments"]]
            assert all(secret in call_record["message"] for secret in call_secrets)
            call_payload = _tool_log_payload(call_record["message"])
            assert {key: call_payload[key] for key in tool_case["metadata"]} == tool_case["metadata"]
            assert all(secret in result_record["message"] for secret in TOOL_RESULT_SECRETS)
            assert all(key in result_record["message"] for key in ("items", "chat_history", "knowledge_base", "error"))

            stored_logs = [await system_log_crud.create(session, obj_in=_system_log_from_record(record)) for record in captured]
    finally:
        logger.remove(sink_id)

    assert len(stored_logs) == 2
    by_kind = {next(key for key in ("tool_call", "tool_result") if key in json.loads(log.extra)): log for log in stored_logs}
    call_log = by_kind["tool_call"]
    result_log = by_kind["tool_result"]
    assert (call_log.uid, call_log.session_id, call_log.level) == ("tool-user", "tool-session", "INFO")
    assert (result_log.uid, result_log.session_id, result_log.level) == ("tool-user", "tool-session", "INFO")

    for log, record, secrets in (
        (call_log, call_record, call_secrets),
        (result_log, result_record, TOOL_RESULT_SECRETS),
    ):
        if enabled:
            assert LOG_REDACTION_PLACEHOLDER in log.message
            assert all(secret not in log.message for secret in secrets)
            assert log.message != record["message"]
            if "tool_call" in json.loads(log.extra):
                persisted_payload = _tool_log_payload(log.message)
                assert {key: persisted_payload[key] for key in tool_case["metadata"]} == tool_case["metadata"]
        else:
            assert log.message == record["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("redaction-off", "redaction-on"))
async def test_formatted_exception_stack_and_extra_follow_redaction_setting(
    log_session_factory: async_sessionmaker[AsyncSession],
    enabled: bool,
) -> None:
    try:
        raise RuntimeError("password=stack-secret; detail=stack-detail-retained")
    except RuntimeError:
        exception_type, exception_value, exception_traceback = sys.exc_info()

    raw_body = "original-body-retained password=body-secret"
    raw_message = LogManager._format_db_log_message(
        raw_body,
        SimpleNamespace(
            type=exception_type,
            value=exception_value,
            traceback=exception_traceback,
        ),
    )
    raw_extra = json.dumps(
        {"password": "extra-secret", "stack_content": "extra-stack-retained"},
        separators=(",", ":"),
    )

    async with log_session_factory() as session:
        if enabled:
            session.add(SystemSetting(key="log_redaction_enabled", value="true"))
            await session.commit()
        log = await system_log_crud.create(
            session,
            obj_in=SystemLogCreate(
                level="ERROR",
                module="exception-stack",
                message=raw_message,
                uid="stack-user",
                session_id="stack-session",
                extra=raw_extra,
                created_at=get_local_time(),
            ),
        )

    if enabled:
        assert f"password={LOG_REDACTION_PLACEHOLDER}" in log.message
        assert "original-body-retained" in log.message
        assert "stack-detail-retained" in log.message
        assert "Traceback (most recent call last)" in log.message
        assert "RuntimeError:" in log.message
        assert all(secret not in log.message for secret in ("body-secret", "stack-secret"))
        assert json.loads(log.extra) == {
            "password": LOG_REDACTION_PLACEHOLDER,
            "stack_content": "extra-stack-retained",
        }
    else:
        assert log.message == raw_message
        assert log.extra == raw_extra


@pytest.mark.asyncio
async def test_writer_reads_redaction_setting_fresh_after_other_sessions_update_it(
    log_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with log_session_factory() as seed_session:
        seed_session.add(SystemSetting(key="log_redaction_enabled", value="false"))
        await seed_session.commit()

    async with log_session_factory() as writer_session:
        cached_setting = await system_setting_crud.get_by_key(writer_session, "log_redaction_enabled")
        assert cached_setting is not None and cached_setting.value == "false"
        await writer_session.commit()

        async with log_session_factory() as settings_session:
            settings = await system_setting_crud.get_runtime_settings(settings_session)
            settings.log_redaction_enabled = True
            await system_setting_crud.update_runtime_settings(settings_session, settings)

        assert cached_setting.value == "false"
        redacted_log = await system_log_crud.create(
            writer_session,
            obj_in=SystemLogCreate(
                level="INFO",
                module="stale-setting-writer-enabled",
                message="password=secret",
                uid="writer-user",
                session_id="writer-session",
                created_at=get_local_time(),
            ),
        )
        assert redacted_log.message == f"password={LOG_REDACTION_PLACEHOLDER}"
        assert "secret" not in redacted_log.message

        async with log_session_factory() as settings_session:
            settings = await system_setting_crud.get_runtime_settings(settings_session)
            settings.log_redaction_enabled = False
            await system_setting_crud.update_runtime_settings(settings_session, settings)

        assert cached_setting.value == "false"
        original_log = await system_log_crud.create(
            writer_session,
            obj_in=SystemLogCreate(
                level="INFO",
                module="stale-setting-writer-disabled",
                message="password=secret",
                uid="writer-user",
                session_id="writer-session",
                created_at=get_local_time(),
            ),
        )
        assert original_log.message == "password=secret"
