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
            "Manage persistent subagents in the current session and profile. A create message must be a self-contained instruction; "
            "the subagent does not read the main conversation history. Ownership is fixed to the current uid, session, and Profile "
            "and cannot be supplied in arguments. The subagent_id stays stable across rounds, while each round receives a new work_id. "
            "Successful create and send_message suspend the main conversation and resume it automatically when the child result arrives; "
            "query and cancel are immediate. Messages sent while a round is running are read before its next model call. If a round is "
            "interrupted, send 'continue previous task' to resume it; after completion, send a message with the same subagent_id to start "
            "a new round. An unknown result does not prove that side effects did not occur. A subagent cannot create another subagent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "query", "send_message", "cancel"],
                    "description": "Operation to perform: create, query, send_message, or cancel.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "description": ("Required for create and send_message. For create, provide a self-contained instruction that does not rely on the main conversation history. For send_message, this is read before the subagent's next model call; use 'continue previous task' to resume an interrupted round."),
                },
                "subagent_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32,
                    "description": "Required for query, send_message, and cancel. Use the stable id returned by create.",
                },
                "work_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional for query and cancel; identifies a specific positive work round.",
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
