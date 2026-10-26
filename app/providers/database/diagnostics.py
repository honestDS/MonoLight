import asyncio
import inspect
import json
import os
import re
import sqlite3
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.log import get_logger
from app.core.paths import ROOT_DIR

_OBSERVER_ATTR = "_monolight_sqlite_diagnostics_observer"
_STATE_KEY = "_monolight_sqlite_diagnostics_transaction"
_CONNECTION_KEY = "_monolight_sqlite_diagnostics_connection"
_SESSION_KEY = "_monolight_sqlite_diagnostics_session"
_DIAGNOSTICS_FILE = "app/providers/database/diagnostics.py"
_CLIENT_FILE = "app/providers/database/client.py"
_DML = frozenset(("insert", "update", "delete", "replace"))
_DDL = frozenset(("create", "alter", "drop", "reindex", "vacuum", "attach", "detach"))
_WRITES = _DML | _DDL | frozenset(("begin_immediate", "begin_exclusive"))
_WORD = re.compile(r"[A-Za-z]+")
_SQL_PREFIX = re.compile(r"(?:\s+|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/\s*)*", re.DOTALL)


@dataclass
class _SessionBinding:
    observer_ref: weakref.ReferenceType[Any]
    session_id: str
    owner_task: weakref.ReferenceType[Any] | None
    owner_task_id: int | None
    source: list[dict[str, Any]]


@dataclass
class _Transaction:
    driver: Any
    connection_id: str
    transaction_id: str
    started_at: float
    owner_task: weakref.ReferenceType[Any] | None
    owner_task_id: int | None
    execution_task: weakref.ReferenceType[Any] | None
    execution_task_id: int | None
    source: list[dict[str, Any]]
    session_id: str | None = None
    pending: dict[str, Any] | None = None
    operation_started_at: float | None = None
    last_successful: dict[str, Any] | None = None
    write_attempted: bool = False
    write_completed_at: float | None = None
    long_logged: bool = False
    timer: asyncio.TimerHandle | None = None


def _frame_location(frame: Any) -> dict[str, Any] | None:
    if frame is None:
        return None
    try:
        path = os.path.abspath(frame.f_code.co_filename)
        relative = os.path.relpath(path, ROOT_DIR).replace("\\", "/")
    except (OSError, TypeError, ValueError):
        return None
    if relative in {_DIAGNOSTICS_FILE, _CLIENT_FILE} or not (relative == "app" or relative.startswith("app/") or relative == "tests" or relative.startswith("tests/")):
        return None
    return {"file": relative, "line": int(frame.f_lineno), "function": str(frame.f_code.co_name)}


def _source() -> list[dict[str, Any]]:
    frame = inspect.currentframe()
    locations: list[dict[str, Any]] = []
    try:
        frame = frame.f_back if frame else None
        for _ in range(64):
            if frame is None or len(locations) >= 12:
                break
            location = _frame_location(frame)
            if location:
                locations.append(location)
            frame = frame.f_back
    finally:
        frame = None
    return locations


def _task_parts() -> tuple[weakref.ReferenceType[Any] | None, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return None, None
    return weakref.ref(task), id(task)


def _await_chain(task: Any) -> list[dict[str, Any]]:
    if task is None:
        return []
    result: list[dict[str, Any]] = []
    coroutine = task.get_coro()
    for _ in range(24):
        if coroutine is None:
            break
        frame = getattr(coroutine, "cr_frame", None) or getattr(coroutine, "gi_frame", None) or getattr(coroutine, "ag_frame", None)
        location = _frame_location(frame)
        if location:
            result.append(location)
        coroutine = getattr(coroutine, "cr_await", None) or getattr(coroutine, "gi_yieldfrom", None) or getattr(coroutine, "ag_await", None)
    return result


def _task_payload(task_ref: weakref.ReferenceType[Any] | None, task_id: int | None) -> dict[str, Any] | None:
    if task_ref is None and task_id is None:
        return None
    return {"task_id": task_id, "await": _await_chain(task_ref() if task_ref else None)}


def _operation(statement: Any, context: Any) -> dict[str, Any]:
    compiled = getattr(context, "compiled", None)
    compiled_statement = getattr(compiled, "statement", None)
    operation_type = "insert" if getattr(compiled, "isinsert", False) else "update" if getattr(compiled, "isupdate", False) else "delete" if getattr(compiled, "isdelete", False) else None
    target_object = getattr(compiled, "dml_table", None)
    if target_object is None:
        target_object = getattr(compiled_statement, "table", None)
    if target_object is None:
        target_object = getattr(compiled_statement, "target", None)
    table_name = getattr(target_object, "name", None)
    target = table_name if isinstance(table_name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", table_name) else None
    if operation_type is None:
        text = statement[:4096] if isinstance(statement, str) else ""
        start = _SQL_PREFIX.match(text).end()
        match = _WORD.match(text, start)
        word = match.group(0).lower() if match else None
        if word in _DML or word in _DDL or word in {"select", "pragma", "with", "commit", "rollback"}:
            operation_type = word
        elif word == "begin":
            match = re.match(r"\s+(immediate|exclusive)\b", text[start + len(word) :], re.IGNORECASE)
            operation_type = f"begin_{match.group(1).lower()}" if match else "begin"
        else:
            operation_type = "unknown"
    return {"type": operation_type, "target": target}


def _driver_transaction(driver: Any) -> bool | None:
    if driver is None:
        return None
    try:
        return bool(driver.in_transaction)
    except (AttributeError, ValueError, sqlite3.Error):
        return None


class SQLiteDiagnosticObserver:
    def __init__(self, sync_engine: Any, slow_transaction_seconds: float) -> None:
        self._pid = os.getpid()
        self._slow_seconds = slow_transaction_seconds
        self._database_path = sync_engine.url.database
        self._active: dict[str, _Transaction] = {}
        setattr(sync_engine, _OBSERVER_ATTR, self)
        event.listen(sync_engine, "begin", lambda connection: self._safe(self._new_state, connection))
        event.listen(sync_engine, "before_cursor_execute", lambda *args: self._safe(self._before, *args))
        event.listen(sync_engine, "after_cursor_execute", lambda *args: self._safe(self._after, *args))
        event.listen(sync_engine, "commit", lambda connection: self._safe(self._requested, connection, "commit_requested"))
        event.listen(sync_engine, "rollback", lambda connection: self._safe(self._requested, connection, "rollback_requested"))
        event.listen(sync_engine, "handle_error", lambda context: self._safe(self._error, context))
        for name in ("checkin", "invalidate", "close"):
            event.listen(sync_engine.pool, name, lambda dbapi, record, *args, event_name=name: self._safe(self._pool, dbapi, record, event_name))

    @staticmethod
    def _safe(callback: Any, *args: Any) -> None:
        try:
            callback(*args)
        except Exception:
            pass

    @staticmethod
    def _connection_info(connection: Any) -> dict[str, Any] | None:
        if connection is None or connection.closed or connection.invalidated:
            return None
        return connection.info

    def _state(self, connection: Any) -> _Transaction | None:
        info = self._connection_info(connection)
        state = info.get(_STATE_KEY) if info else None
        return state if isinstance(state, _Transaction) else None

    def _new_state(self, connection: Any) -> _Transaction | None:
        info = self._connection_info(connection)
        if info is None:
            return None
        driver = connection.connection.driver_connection
        if driver is None:
            return None
        old = info.get(_STATE_KEY)
        if isinstance(old, _Transaction):
            self._cleanup(old, info)
        owner, owner_id = _task_parts()
        connection_id = info.get(_CONNECTION_KEY)
        if not isinstance(connection_id, str):
            connection_id = f"{self._pid}-connection-{uuid.uuid4().hex}"
            info[_CONNECTION_KEY] = connection_id
        state = _Transaction(driver, connection_id, f"{self._pid}-transaction-{uuid.uuid4().hex}", time.monotonic(), owner, owner_id, owner, owner_id, _source())
        info[_STATE_KEY] = state
        self._active[connection_id] = state
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return state
        state.timer = loop.call_later(self._slow_seconds, self._safe, self._long, weakref.ref(state))
        return state

    def _cleanup(self, state: _Transaction, info: dict[str, Any] | None = None) -> None:
        self._active.pop(state.connection_id, None)
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if info is not None and info.get(_STATE_KEY) is state:
            info.pop(_STATE_KEY, None)

    @staticmethod
    def _touch(state: _Transaction) -> None:
        task_ref, task_id = _task_parts()
        if task_ref is not None:
            state.execution_task, state.execution_task_id = task_ref, task_id

    def _payload(self, name: str, state: _Transaction | None) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "event": name,
            "pid": self._pid,
            "database_path": self._database_path,
            "connection_id": state.connection_id if state else None,
            "transaction_id": state.transaction_id if state else None,
            "session_id": state.session_id if state else None,
            "driver_in_transaction": _driver_transaction(state.driver if state else None),
            "transaction_age_seconds": max(0.0, now - state.started_at) if state else None,
            "post_write_seconds": max(0.0, now - state.write_completed_at) if state and state.write_completed_at else None,
            "operation_wait_seconds": max(0.0, now - state.operation_started_at) if state and state.operation_started_at else None,
            "source": state.source if state else [],
            "owner_task": _task_payload(state.owner_task, state.owner_task_id) if state else None,
            "execution_task": _task_payload(state.execution_task, state.execution_task_id) if state else None,
            "last_successful_operation": state.last_successful if state else None,
            "pending_operation": state.pending if state else None,
        }

    @staticmethod
    def _emit(payload: dict[str, Any]) -> None:
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        get_logger(__name__).bind(sqlite_diagnostic=True).debug(f"[sqlite-debug] {message}")

    def _before(self, connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: bool) -> None:
        state = self._state(connection) or self._new_state(connection)
        if state is None:
            return
        self._touch(state)
        state.pending = _operation(statement, context)
        state.operation_started_at = time.monotonic()
        if state.pending["type"] in _WRITES and not state.write_attempted:
            state.write_attempted = True
            payload = self._payload("write_wait", state)
            payload["wait_seconds"] = 0.0
            self._emit(payload)

    def _after(self, connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: bool) -> None:
        state = self._state(connection)
        if state is None:
            return
        self._touch(state)
        operation = state.pending or _operation(statement, context)
        wait_seconds = max(0.0, time.monotonic() - state.operation_started_at) if state.operation_started_at else None
        state.last_successful, state.pending, state.operation_started_at = operation, None, None
        if operation["type"] in _WRITES and state.write_completed_at is None:
            state.write_completed_at = time.monotonic()
            if state.timer is not None:
                state.timer.cancel()
            state.long_logged = False
            # 长读后转写需重新采样等待栈。
            state.timer = asyncio.get_running_loop().call_later(self._slow_seconds, self._safe, self._long, weakref.ref(state))
            payload = self._payload("write_completed", state)
            payload["wait_seconds"] = wait_seconds
            self._emit(payload)

    def _requested(self, connection: Any, name: str) -> None:
        state = self._state(connection)
        if state:
            self._touch(state)
            if state.write_attempted or state.long_logged:
                self._emit(self._payload(name, state))

    def _long(self, state_ref: weakref.ReferenceType[_Transaction]) -> None:
        state = state_ref()
        if state is None or state.connection_id not in self._active or state.long_logged:
            return
        state.long_logged, state.timer = True, None
        self._emit(self._payload("long_transaction", state))

    def _snapshots(self) -> dict[str, Any]:
        states = sorted(self._active.values(), key=lambda item: (item.write_completed_at is not None, -item.started_at), reverse=True)
        return {
            "active_transaction_count": len(states),
            "snapshot_truncated": len(states) > 32,
            "active_transactions": [self._payload("active_transaction", state) for state in states[:32]],
        }

    def _error(self, exception_context: Any) -> None:
        exception = exception_context.original_exception
        code = getattr(exception, "sqlite_errorcode", None) if isinstance(exception, sqlite3.Error) else None
        if not isinstance(code, int) or code & 0xFF not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return
        error_name = getattr(exception, "sqlite_errorname", None)
        name = error_name if isinstance(error_name, str) else None
        connection = getattr(exception_context, "connection", None)
        state = self._state(connection)
        if state:
            self._touch(state)
        operation = state.pending if state and state.pending else _operation(exception_context.statement, exception_context.execution_context)
        payload = self._payload("lock_error", state)
        payload["operation"] = operation
        payload["wait_seconds"] = payload["operation_wait_seconds"]
        payload["sqlite_error_code"] = code
        payload["sqlite_error_name"] = name
        payload.update(self._snapshots())
        self._emit(payload)

    def _pool(self, dbapi_connection: Any, record: Any, event_name: str) -> None:
        info = record.info
        state = info.get(_STATE_KEY)
        if isinstance(state, _Transaction) and state.connection_id in self._active:
            if event_name == "checkin" and (state.write_attempted or state.long_logged):
                self._emit(self._payload("connection_returned", state))
            self._cleanup(state, info)
        if event_name != "checkin":
            info.pop(_CONNECTION_KEY, None)

    def bind_session(self, binding: _SessionBinding, connection: Any) -> None:
        state = self._state(connection) or self._new_state(connection)
        if state is None:
            return
        state.session_id, state.source = binding.session_id, binding.source
        if binding.owner_task is not None:
            state.owner_task, state.owner_task_id = binding.owner_task, binding.owner_task_id
        self._touch(state)


def install_sqlite_diagnostics(async_engine: AsyncEngine, *, slow_transaction_seconds: float = 5.0) -> SQLiteDiagnosticObserver | None:
    if async_engine.dialect.name != "sqlite":
        return None
    sync_engine = async_engine.sync_engine
    existing = getattr(sync_engine, _OBSERVER_ATTR, None)
    if isinstance(existing, SQLiteDiagnosticObserver):
        return existing
    return SQLiteDiagnosticObserver(sync_engine, slow_transaction_seconds)


def observe_sqlite_session(async_session: AsyncSession) -> _SessionBinding | None:
    sync_session = async_session.sync_session
    bind = sync_session.bind
    if bind is None:
        return None
    observer = getattr(bind.engine, _OBSERVER_ATTR, None)
    if not isinstance(observer, SQLiteDiagnosticObserver):
        return None
    session_info = sync_session.info
    existing = session_info.get(_SESSION_KEY)
    if isinstance(existing, _SessionBinding) and existing.observer_ref() is observer:
        return existing
    owner, owner_id = _task_parts()
    binding = _SessionBinding(weakref.ref(observer), f"{observer._pid}-session-{id(sync_session)}", owner, owner_id, _source())

    def after_begin(session: Any, transaction: Any, connection: Any) -> None:
        current = binding.observer_ref()
        if current is not None:
            current._safe(current.bind_session, binding, connection)

    session_info[_SESSION_KEY] = binding
    event.listen(sync_session, "after_begin", after_begin)
    return binding
