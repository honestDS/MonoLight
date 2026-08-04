import hashlib
import json
from datetime import datetime
from typing import Any

from app.core.constants import (
    ERR_INTERNAL_SERVER_ERROR,
    ERR_MEMORY_ENUM_INVALID,
    ERR_MEMORY_FIELD_REQUIRED,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    ERR_TOOL_UNSUPPORTED_ARGUMENTS,
    MEMORY_CHANGE_EVIDENCE_MAX_CHARS,
    MEMORY_CONTENT_MAX_CHARS,
    MEMORY_KEY_MAX_CHARS,
    MEMORY_SCOPE_MAX_CHARS,
)
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.tools.base import BaseExecutor
from app.models.memory import LongTermMemorySource

MANAGE_LONGTERM_MEMORY_TOOL_NAME = "manage_longterm_memory"

MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        "description": "Recall and manage the user's long-term memories.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["recall", "create", "update", "delete"],
                    "description": "The memory operation to perform.",
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_CONTENT_MAX_CHARS,
                    "description": "The full semantic query for recall.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Optional maximum number of recall results.",
                },
                "memory_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The memory record ID returned by recall.",
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "The current memory version required for an update or delete.",
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_CONTENT_MAX_CHARS,
                    "description": "The complete memory content.",
                },
                "memory_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_KEY_MAX_CHARS,
                    "description": "A stable key identifying the memory.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "project", "todo", "constraint"],
                    "description": "The type of memory.",
                },
                "importance": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "description": "The importance from 0 to 10.",
                },
                "scope": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_SCOPE_MAX_CHARS,
                    "description": "Optional applicability scope.",
                },
                "change_evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_CHANGE_EVIDENCE_MAX_CHARS,
                    "description": "The user's explicit evidence for a memory change.",
                },
                "suppress_current": {
                    "type": "boolean",
                    "default": False,
                    "description": "Temporarily suppress the current version while an update is processed.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}

_OPERATION_FIELDS = {
    "recall": {"operation", "query", "top_k"},
    "create": {"operation", "content", "memory_key", "memory_type", "importance", "scope", "change_evidence"},
    "update": {
        "operation",
        "memory_id",
        "expected_version",
        "content",
        "memory_key",
        "memory_type",
        "importance",
        "scope",
        "change_evidence",
        "suppress_current",
    },
    "delete": {"operation", "memory_id", "expected_version"},
}
_REQUIRED_FIELDS = {
    "recall": ("query",),
    "create": ("content", "memory_key", "memory_type", "importance"),
    "update": ("memory_id", "expected_version", "content", "memory_key", "memory_type", "importance"),
    "delete": ("memory_id",),
}


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _json_result(operation: Any, status: str, **payload: Any) -> str:
    return json.dumps({"operation": operation, "status": status, **payload}, ensure_ascii=False)


def _field_error(field: str) -> str:
    return t(ERR_MEMORY_FIELD_REQUIRED, field=field)


def validate_longterm_memory_arguments(arguments: dict[str, Any]) -> tuple[str | None, str | None]:
    operation = arguments.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        return operation if isinstance(operation, str) else None, _field_error("operation")
    if operation not in _OPERATION_FIELDS:
        return operation, t(ERR_MEMORY_ENUM_INVALID, field="operation")

    unsupported = sorted(set(arguments) - _OPERATION_FIELDS[operation])
    if unsupported:
        return operation, t(
            ERR_TOOL_UNSUPPORTED_ARGUMENTS,
            tool_name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
            fields=", ".join(unsupported),
        )

    missing = [field for field in _REQUIRED_FIELDS[operation] if field not in arguments]
    if missing:
        return operation, _field_error(", ".join(missing))
    if operation == "recall" and (not isinstance(arguments.get("query"), str) or not arguments["query"].strip()):
        return operation, _field_error("query")
    return operation, None


def _stable_digest(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_memory_service() -> Any:
    from app.core.memory.service import memory_service

    return memory_service


class LongTermMemoryExecutor(BaseExecutor):
    requires_audit = False

    def _has_mutation_runtime_context(self, memory_config: Any) -> bool:
        profile_id = getattr(self.profile, "id", None)
        dispatch_context = self.dispatch_context
        tool_call_id = getattr(dispatch_context, "tool_call_id", None)
        source_message_id = getattr(dispatch_context, "source_message_id", None)
        if source_message_id is None:
            source_message_id = self.source_message_id
        return (
            self.db is not None
            and memory_config is not None
            and isinstance(profile_id, int)
            and not isinstance(profile_id, bool)
            and profile_id > 0
            and isinstance(self.session_id, str)
            and bool(self.session_id.strip())
            and dispatch_context is not None
            and isinstance(tool_call_id, str)
            and bool(tool_call_id.strip())
            and isinstance(source_message_id, int)
            and not isinstance(source_message_id, bool)
            and source_message_id > 0
        )

    def _runtime_source_id(self, operation: str) -> str:
        tool_call_id = self.dispatch_context.tool_call_id if self.dispatch_context else None
        if isinstance(tool_call_id, str) and tool_call_id.strip() and len(tool_call_id) <= 255:
            return tool_call_id.strip()
        return _stable_digest(self.uid, self.session_id, tool_call_id, operation)

    def _runtime_source_message_id(self) -> int | None:
        source_message_id = getattr(self.dispatch_context, "source_message_id", None)
        return self.source_message_id if source_message_id is None else source_message_id

    def _mutation_context(self, operation: str) -> dict[str, Any]:
        tool_call_id = self.dispatch_context.tool_call_id if self.dispatch_context else None
        return {
            "dedupe_key": f"longterm-memory:{_stable_digest(self.uid, self.session_id, tool_call_id, operation)}"[:255],
            "source": LongTermMemorySource.LLM_TOOL,
            "source_id": self._runtime_source_id(operation),
            "source_session_id": self.session_id,
            "source_profile_id": getattr(self.profile, "id", None),
            "source_message_id": self._runtime_source_message_id(),
        }

    def _memory_config(self) -> Any:
        return getattr(self.cfg, "memory", None)

    async def _recall(self, arguments: dict[str, Any], memory_config: Any) -> str:
        memory_service = _get_memory_service()
        effective_top_k = arguments.get("top_k", memory_config.top_k)
        effective_candidate_k = max(memory_config.candidate_k, effective_top_k)
        result = await memory_service.recall(
            db=self.db,
            uid=self.uid,
            query=arguments["query"],
            top_k=effective_top_k,
            candidate_k=effective_candidate_k,
            result_max_chars=memory_config.result_max_chars,
        )
        items = []
        for item in result.items:
            updated_at = item.updated_at.isoformat() if isinstance(item.updated_at, datetime) else item.updated_at
            items.append(
                {
                    "memory_id": item.memory_id,
                    "memory_key": item.memory_key,
                    "content": item.content,
                    "memory_type": _value(item.memory_type),
                    "importance": item.importance,
                    "scope": item.scope,
                    "version": item.version,
                    "updated_at": updated_at,
                    "source": _value(item.source),
                    "dense_distance": item.dense_distance,
                    "dense_rank": item.dense_rank,
                    "sparse_score": item.sparse_score,
                    "sparse_rank": item.sparse_rank,
                    "fusion_score": item.fusion_score,
                    "truncated": item.truncated,
                }
            )
        payload: dict[str, Any] = {"items": items}
        if result.error_key:
            payload["error"] = t(result.error_key)
        return _json_result("recall", _value(result.status), **payload)

    async def _mutate(self, operation: str, arguments: dict[str, Any]) -> str:
        memory_service = _get_memory_service()
        context = self._mutation_context(operation)
        if operation == "create":
            result = await memory_service.create(
                db=self.db,
                uid=self.uid,
                content=arguments["content"],
                memory_key=arguments["memory_key"],
                memory_type=arguments["memory_type"],
                importance=arguments["importance"],
                scope=arguments.get("scope"),
                change_evidence=arguments.get("change_evidence"),
                **context,
            )
        elif operation == "update":
            result = await memory_service.update(
                db=self.db,
                uid=self.uid,
                memory_id=arguments["memory_id"],
                expected_version=arguments["expected_version"],
                content=arguments["content"],
                memory_key=arguments["memory_key"],
                memory_type=arguments["memory_type"],
                importance=arguments["importance"],
                scope=arguments.get("scope"),
                change_evidence=arguments.get("change_evidence"),
                suppress_current=arguments.get("suppress_current", False),
                **context,
            )
        else:
            result = await memory_service.delete(
                db=self.db,
                uid=self.uid,
                memory_id=arguments["memory_id"],
                expected_version=arguments.get("expected_version"),
                **context,
            )

        job = result.job
        current_version = getattr(result.record, "version", None)
        if current_version is None:
            current_version = getattr(job, "expected_version", None)
        return _json_result(
            operation,
            _value(result.status),
            job_id=result.job_id,
            memory_id=result.memory_id,
            current_version=current_version,
        )

    async def execute(self, **kwargs: Any) -> str:
        operation, validation_error = validate_longterm_memory_arguments(kwargs)
        if validation_error:
            return _json_result(operation, "failed", error=validation_error)

        memory_config = self._memory_config()
        if self.db is None or memory_config is None:
            return _json_result(operation, "failed", error=t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        if operation != "recall" and not self._has_mutation_runtime_context(memory_config):
            return _json_result(operation, "failed", error=t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        try:
            if operation == "recall":
                return await self._recall(kwargs, memory_config)
            return await self._mutate(operation, kwargs)
        except BaseBusinessException as exc:
            return _json_result(operation, "failed", error=exc.render_message())
        except Exception:
            return _json_result(operation, "failed", error=t(ERR_INTERNAL_SERVER_ERROR))


__all__ = [
    "MANAGE_LONGTERM_MEMORY_TOOL_NAME",
    "MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA",
    "LongTermMemoryExecutor",
    "validate_longterm_memory_arguments",
]
