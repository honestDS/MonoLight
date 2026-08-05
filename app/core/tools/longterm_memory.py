import hashlib
import json
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
        "description": (
            "Recall and manage the user's long-term memories. "
            "Use a concise normalized retrieval expression for recall, and keep each memory to one "
            "independently maintainable concrete topic and property. "
            "Use separate create calls for different entities, topics, or unrelated facts. "
            "Update only an explicitly corrected or replaced existing memory when its ID, current version, "
            "and concrete topic are bound by trusted context; never merge unrelated topics."
        ),
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
                    "description": (
                        "A concise, normalized long-term-memory retrieval expression containing only entities, topics, and "
                        "stable background relevant to the current request. Do not copy the user's full wording or include "
                        "request actions such as asking, answering, remembering, or saving; do not pile up keywords or invent "
                        "uncertain facts."
                    ),
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
                    "description": "The exact existing memory record ID explicitly provided by the user or bound to the concrete topic in trusted context. Required for update or delete; never infer it from recall results, memory type, scope, memory_key, or semantic similarity. Recall does not return memory IDs.",
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
                    "description": "Complete content for exactly one independently maintainable concrete topic and property. Keep it atomic: do not combine different entities, topics, or unrelated facts. For update, replace only the same existing topic and do not add other topics.",
                },
                "memory_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_KEY_MAX_CHARS,
                    "description": "A narrow, stable semantic key for exactly one atomic memory topic and property. It must identify a single memory, not a broad category or bucket that accumulates multiple facts.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "project", "todo", "constraint"],
                    "description": "Classify this memory from its content itself as fact, preference, project, todo, or constraint. Objective information from tools, searches, or knowledge bases remains objective information and must not be labeled preference solely because the user asks to remember it.",
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
                    "description": "Only a brief reason explicitly stated by the user for correcting, replacing, or changing this same memory. Do not include search results, tool conclusions, assistant summaries, or the full task or request.",
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
        if _value(result.status) != "ok":
            return ""
        return "\n\n".join(item.content for item in result.items)

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
            if operation == "recall":
                return ""
            return _json_result(operation, "failed", error=validation_error)

        memory_config = self._memory_config()
        if self.db is None or memory_config is None:
            if operation == "recall":
                return ""
            return _json_result(operation, "failed", error=t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
        if operation != "recall" and not self._has_mutation_runtime_context(memory_config):
            return _json_result(operation, "failed", error=t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        try:
            if operation == "recall":
                return await self._recall(kwargs, memory_config)
            return await self._mutate(operation, kwargs)
        except BaseBusinessException as exc:
            if operation == "recall":
                return ""
            return _json_result(operation, "failed", error=exc.render_message())
        except Exception:
            if operation == "recall":
                return ""
            return _json_result(operation, "failed", error=t(ERR_INTERNAL_SERVER_ERROR))


__all__ = [
    "MANAGE_LONGTERM_MEMORY_TOOL_NAME",
    "MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA",
    "LongTermMemoryExecutor",
    "validate_longterm_memory_arguments",
]
