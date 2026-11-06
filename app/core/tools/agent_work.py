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

__all__ = ["AGENT_WORK_TOOL_SCHEMA", "AgentWorkExecutor"]


AGENT_WORK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": AGENT_WORK_TOOL_NAME,
        "description": (
            "When to use a subagent\n"
            "Delegate a clearly scoped, multi-step task with an independent deliverable, such as multi-source research and fact-checking, comparing alternatives, "
            "focused analysis or review, changes confined to a file or module, or an investigation that needs follow-up. Use ordinary tools directly for simple "
            "lookups, reads, or small edits. Keep dependent steps with the same subagent. Independent tasks can be delegated separately, with non-overlapping file edits.\n\n"
            "Creating a task\n"
            "Use create with the goal, necessary background, inputs or file paths, constraints, and expected deliverable. The subagent does not automatically read this conversation, so make the task self-contained. "
            "Specify whether it should only investigate or may make changes, and what conclusions, evidence, or files it should return.\n\n"
            "Continuing and following up\n"
            'Reuse the returned subagent_id with send_message to add instructions, continue interrupted work, or request follow-up work. For a simple continuation, send "continue previous task".\n\n'
            "Waiting and checking status\n"
            "After create or send_message is accepted, this conversation waits and resumes automatically with the result. There is no need to poll query while waiting. "
            "Use query to inspect status, saved results, or an interruption reason, and cancel to stop work that is no longer needed. Summarize the returned results for the user.\n\n"
            "Important boundaries\n"
            "Cancelling does not undo earlier changes. If execution is reported as unknown, verify what actually happened before repeating an action. Use only subagent_id values returned in this conversation under the current Profile. A subagent cannot create other subagents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "query", "send_message", "cancel"],
                    "description": "create: delegate a new task; send_message: add instructions or continue work with an existing subagent; query: inspect work; cancel: stop unneeded work.",
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
                    "description": "Required for query, send_message, and cancel. Copy the subagent_id returned by create and reuse it for follow-up work with that subagent.",
                },
                "work_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "For query and cancel only. Copy a returned work_id to inspect or cancel that particular task run; omit it to query the latest work or cancel the current or latest work.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


class CreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class SendArguments(CreateArguments):
    subagent_id: str = Field(min_length=1, max_length=32)


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    subagent_id: str = Field(min_length=1, max_length=32)
    work_id: int | None = Field(default=None, gt=0)


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


def _failure(operation: Any, error: str, details: list[dict[str, str]] | None = None) -> str:
    result: dict[str, Any] = {
        "status": "failed",
        "operation": operation,
        "error": error,
    }
    if details:
        result["details"] = details
    return json.dumps(result, ensure_ascii=False)


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class AgentWorkExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, operation: str, **arguments: Any) -> str:
        argument_model: type[BaseModel] | None
        if operation == "create":
            argument_model = CreateArguments
        elif operation == "send_message":
            argument_model = SendArguments
        elif operation in {"query", "cancel"}:
            argument_model = LookupArguments
        else:
            return _failure(
                operation,
                t(ERR_SUBAGENT_ARGUMENTS_INVALID),
                [{"field": "operation", "type": "invalid_operation"}],
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return _failure(
                operation,
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
            return _failure(operation, t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        from app.core.session_reply_queue.child_work import (
            cancel_subagent,
            create_subagent,
            query_subagent,
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
                    message=parsed.message,
                )
            elif operation == "query":
                result = await query_subagent(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    subagent_id=parsed.subagent_id,
                    work_id=parsed.work_id,
                )
            elif operation == "send_message":
                result = await send_subagent_message(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    tool_call_id=tool_call_id,
                    subagent_id=parsed.subagent_id,
                    message=parsed.message,
                )
            else:
                result = await cancel_subagent(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    subagent_id=parsed.subagent_id,
                    work_id=parsed.work_id,
                )
        except ParameterException as exc:
            await db.rollback()
            return _failure(
                operation,
                t(exc.message, default=exc.message, **exc.kwargs),
            )

        return json.dumps(result, ensure_ascii=False)
