import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_ARGUMENTS_INVALID,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
)
from app.core.exceptions import ParameterException
from app.core.i18n import t

from .base import BaseExecutor

__all__ = ["AGENT_WORK_TOOL_SCHEMA", "SUBAGENT_BOARD_TOOL_SCHEMA", "AgentWorkExecutor"]


AGENT_WORK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": AGENT_WORK_TOOL_NAME,
        "description": (
            "When to use a subagent\n"
            "Prefer delegating independently deliverable parts when work involves broad investigation, large volumes of material, multi-source verification, comparing plans or alternatives, independent checking, or context isolation. Delegate before completing that work, not after. "
            "Use ordinary tools directly for simple lookups and reads. Dispatch independent create operations in the same tool batch so they can run in parallel; for dependent work, wait for the relevant result before using send_message.\n\n"
            "Creating a task\n"
            "Use create with a short, human-readable name and the goal, necessary background, inputs or file paths, constraints, and expected deliverable. The subagent has no access to this conversation's history, so make the task self-contained. "
            "It may use Shell (non_interactive or interactive), terminal_tool, and watch when enabled by the current Profile; actual availability and use remain governed by Profile enablement, audit decisions, and directory rules. "
            "Specify whether it should only investigate or may make changes, and what conclusions, evidence, or files it should return.\n\n"
            "Continuing and following up\n"
            'Reuse the returned subagent_id with send_message to add instructions, continue interrupted work, or request follow-up work. For a simple continuation, send "continue previous task".\n\n'
            "Status and board history\n"
            "After the tool returns a queued confirmation, the framework saves the current batch result and "
            "checkpoint, then automatically pauses the parent reply. Board updates and completion, failure, or "
            "cancellation wake the parent for review. If no stage adjustment is needed and the task is still "
            "unfinished, call end_session(skip_reply=true) to wait silently again; once all tasks are complete, "
            "verify and summarize the results. "
            "Use list to view system status for unfinished tasks in the current user, session, and Profile scope; completed subagents are omitted. "
            "Use board_read to read stage progress for one scoped subagent, check direction, and correct course; it does not replace the complete delivered result. "
            "Use query_result to retrieve only the complete result saved for one finished work round, or its failure or cancellation reason. Do not use it to view progress or poll. "
            "With an explicit work_id, query that work round; without one, query the latest work round. "
            "Use board_post from the currently executing subagent task to record its own progress; it returns immediately so execution can continue, and its identity comes from the current execution context rather than user-supplied identifiers. "
            "Use cancel to stop work that is no longer needed. Work results use status for the work-round state, ended_without_reply for a normal end without a report, reason for the work-round ending reason, and error when the call itself fails. Summarize the returned results for the user.\n\n"
            "Important boundaries\n"
            "board_post and board_read are limited to the current user, session, and Profile. Cancelling does not undo earlier changes. If execution is reported as unknown, verify what actually happened before repeating an action. "
            "Do not access subagents across users, sessions, or Profiles, or create nested subagents. Use only subagent_id values actually returned in this conversation under the current Profile."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "list", "query_result", "send_message", "cancel", "board_post", "board_read"],
                    "description": (
                        "create: delegate a new task; list: view system status for unfinished tasks in the current scope; "
                        "query_result: retrieve only the complete result saved for one finished work round, or its failure or cancellation reason; do not use it to view progress or poll; "
                        "send_message: add instructions or continue work with an existing subagent; "
                        "cancel: stop unneeded work; board_post: record progress for the currently executing subagent task and return immediately; "
                        "board_read: read stage progress for one subagent to check direction and correct course; it does not replace the complete delivered result."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "pattern": "^[^\\r\\n\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029]+$",
                    "description": "Required only for create. A short, single-line, human-readable task label; it does not change subagent_id-based targeting.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Required for create and send_message. For create, include the goal, needed context, inputs or paths, constraints, and expected result; "
                        "the subagent cannot read this conversation. For send_message, explain what to add, change, or continue; use 'continue previous task' "
                        "if no new instruction is needed."
                    ),
                },
                "subagent_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "description": "Required for query_result, send_message, cancel, and board_read. Copy the subagent_id actually returned by create or list and reuse it for work with that subagent.",
                },
                "work_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "For query_result and cancel only. Copy a returned work_id to target a specific work round; omit it to query the latest round or cancel the current round.",
                },
                "stage": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Required for board_post only. A non-empty progress stage label, up to 64 characters.",
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Required for board_post only. A non-empty progress update with its meaningful formatting preserved.",
                },
            },
            "required": ["operation"],
            "anyOf": [
                {"properties": {"operation": {"const": "create"}}, "required": ["name", "message"]},
                {"properties": {"operation": {"const": "list"}}, "additionalProperties": False},
                {"properties": {"operation": {"const": "query_result"}}, "required": ["subagent_id"]},
                {"properties": {"operation": {"const": "send_message"}}, "required": ["subagent_id", "message"]},
                {"properties": {"operation": {"const": "cancel"}}, "required": ["subagent_id"]},
                {
                    "properties": {
                        "operation": {"const": "board_post"},
                        "stage": {},
                        "content": {},
                    },
                    "required": ["stage", "content"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "operation": {"const": "board_read"},
                        "subagent_id": {},
                    },
                    "required": ["subagent_id"],
                    "additionalProperties": False,
                },
            ],
            "additionalProperties": False,
        },
    },
}


SUBAGENT_BOARD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": AGENT_WORK_TOOL_NAME,
        "description": ("Use board_post to record progress for the current subagent task. The framework determines the subagent identity and work round. After posting, continue the task immediately; a board post does not replace the final report. Do not create nested subagents or specify another identity."),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["board_post"],
                    "description": "Record progress for the current subagent task.",
                },
                "stage": AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["stage"],
                "content": AGENT_WORK_TOOL_SCHEMA["function"]["parameters"]["properties"]["content"],
            },
            "required": ["operation", "stage", "content"],
            "additionalProperties": False,
        },
    },
}


class _MessageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class CreateArguments(_MessageArguments):
    name: str = Field(min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value):
            raise ValueError("name must not contain control characters")
        return value


class SendArguments(_MessageArguments):
    subagent_id: str = Field(min_length=1, max_length=32)


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    subagent_id: str = Field(min_length=1, max_length=32)
    work_id: int | None = Field(default=None, gt=0)


class ListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BoardPostArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stage: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)

    @field_validator("stage", "content")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class BoardReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    subagent_id: str = Field(min_length=1, max_length=32)


def _validation_details(error: ValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = item.get("loc", ())
        details.append(
            {
                "field": ".".join(str(part) for part in location),
                "type": str(item.get("type", "invalid")),
            }
        )
    return details


def _failure(error: str, details: list[dict[str, str]] | None = None) -> str:
    result: dict[str, Any] = {"error": error}
    if details:
        result["details"] = details
    return json.dumps(result, ensure_ascii=False)


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class AgentWorkExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, operation: str, **arguments: Any) -> str:
        argument_model: type[BaseModel] | None = None
        if operation == "create":
            argument_model = CreateArguments
        if operation == "list":
            argument_model = ListArguments
        if operation == "send_message":
            argument_model = SendArguments
        if operation in {"query_result", "cancel"}:
            argument_model = LookupArguments
        if operation == "board_post":
            argument_model = BoardPostArguments
        if operation == "board_read":
            argument_model = BoardReadArguments
        if argument_model is None:
            return _failure(
                t(ERR_SUBAGENT_ARGUMENTS_INVALID),
                [{"field": "operation", "type": "invalid_operation"}],
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return _failure(
                t(ERR_SUBAGENT_ARGUMENTS_INVALID),
                _validation_details(exc),
            )

        dispatch_context = self.dispatch_context
        db = getattr(dispatch_context, "db", None) if dispatch_context is not None else None
        uid = getattr(dispatch_context, "uid", None) if dispatch_context is not None else None
        session_id = getattr(dispatch_context, "session_id", None) if dispatch_context is not None else None
        profile = getattr(dispatch_context, "profile", None) if dispatch_context is not None else None
        profile_id = getattr(profile, "id", None)
        profile_uid = getattr(profile, "uid", None)
        tool_call_id = getattr(dispatch_context, "tool_call_id", None) if dispatch_context is not None else None
        if db is None or not _valid_text(uid) or not _valid_text(session_id) or isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id <= 0 or not _valid_text(profile_uid) or profile_uid != uid or not _valid_text(tool_call_id):
            return _failure(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        from app.core.session_reply_queue.child_work import (
            cancel_subagent,
            create_subagent,
            list_subagents,
            post_subagent_board,
            query_subagent_result,
            read_subagent_board,
            send_subagent_message,
        )

        try:
            if operation == "create":
                result = await create_subagent(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    tool_call_id=tool_call_id,
                    name=parsed.name,
                    message=parsed.message,
                )
            if operation == "list":
                result = await list_subagents(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                )
            if operation == "query_result":
                result = await query_subagent_result(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    subagent_id=parsed.subagent_id,
                    work_id=parsed.work_id,
                )
            if operation == "send_message":
                result = await send_subagent_message(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    tool_call_id=tool_call_id,
                    subagent_id=parsed.subagent_id,
                    message=parsed.message,
                )
            if operation == "cancel":
                result = await cancel_subagent(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    subagent_id=parsed.subagent_id,
                    work_id=parsed.work_id,
                )
            if operation == "board_post":
                result = await post_subagent_board(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    stage=parsed.stage,
                    content=parsed.content,
                )
            if operation == "board_read":
                result = await read_subagent_board(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    subagent_id=parsed.subagent_id,
                )
        except ParameterException as exc:
            await db.rollback()
            return _failure(
                t(exc.message, default=exc.message, **exc.kwargs),
            )

        return json.dumps(result, ensure_ascii=False)
