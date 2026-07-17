import asyncio
import json
import os
import time
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    ERR_BACKGROUND_TASK_UNSUPPORTED,
    ERR_TOOL_ARGUMENT_SCHEMA_INVALID,
    ERR_TOOL_MISSING_REQUIRED_ARGUMENTS,
    ERR_TOOL_NOT_ENABLED,
    ERR_TOOL_NOT_REGISTERED,
    ERR_TOOL_UNSUPPORTED_ARGUMENTS,
    MSG_BACKGROUND_TASK_QUEUED,
)
from app.core.dispatch_context import build_dispatch_context
from app.core.i18n import t
from app.core.log import (
    LogManager,
    get_logger,
)
from app.core.prompts import BACKGROUND_TASK_UNSUPPORTED_PROMPT
from app.core.tools import (
    TOOL_EXECUTOR_MAP,
    get_tool_parameters_schema,
    get_tool_required_parameters,
    tool_runs_in_background,
    tool_schema_has_parameter,
)
from app.core.utils.dispatcher.audit_tool_call import audit_tool_call
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_messages_for_budget
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


def _is_tool_enabled(tool_name: str, cfg: ProfileConfig) -> bool:
    enabled_tools = getattr(getattr(cfg, "tool", None), "enabled_tools", None)
    if not isinstance(enabled_tools, list):
        return False
    return tool_name in {name for name in enabled_tools if isinstance(name, str)}


def _build_tool_disabled_result(tool_name: str) -> str:
    return json.dumps(
        {
            "error": t(ERR_TOOL_NOT_ENABLED, tool_name=tool_name),
            "tool_name": tool_name,
            "status": "failed",
        },
        ensure_ascii=False,
    )


def _build_background_task_queued_result(tool_name: str, task_id: int) -> str:
    return json.dumps(
        {
            "status": "queued",
            "tool_name": tool_name,
            "task_id": task_id,
            "message": t(MSG_BACKGROUND_TASK_QUEUED, tool_name=tool_name),
        },
        ensure_ascii=False,
    )


def _build_background_task_unsupported_result(tool_name: str) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": t(ERR_BACKGROUND_TASK_UNSUPPORTED, tool_name=tool_name),
            "instruction": BACKGROUND_TASK_UNSUPPORTED_PROMPT.format(tool_name=tool_name),
        },
        ensure_ascii=False,
    )


def _build_tool_error_result(tool_name: str, error_message: str) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": error_message,
        },
        ensure_ascii=False,
    )


def _build_missing_required_arguments_result(tool_name: str, missing_arguments: list[str]) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": t(
                ERR_TOOL_MISSING_REQUIRED_ARGUMENTS,
                fields=", ".join(missing_arguments),
            ),
            "missing_arguments": missing_arguments,
        },
        ensure_ascii=False,
    )


def _build_unsupported_arguments_result(tool_name: str, unsupported_arguments: list[str]) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": t(
                ERR_TOOL_UNSUPPORTED_ARGUMENTS,
                tool_name=tool_name,
                fields=", ".join(unsupported_arguments),
            ),
            "unsupported_arguments": unsupported_arguments,
        },
        ensure_ascii=False,
    )


def _schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _schema_type_matches(value, expected_type):
        return [f"{path} must be {expected_type}"]
    if isinstance(expected_type, list) and not any(isinstance(item, str) and _schema_type_matches(value, item) for item in expected_type):
        return [f"{path} has an invalid type"]
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path} is shorter than {schema['minLength']}")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path} is longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path} must be at least {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path} must be at most {schema['maximum']}")
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path} must contain at least {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path} must contain at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_schema_value(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for field in required:
            if isinstance(field, str) and field not in value:
                errors.append(f"{path}.{field} is required")
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    errors.append(f"{path}.{field} is not allowed")
        for field, field_value in value.items():
            field_schema = properties.get(field)
            if isinstance(field_schema, dict):
                errors.extend(_validate_schema_value(field_value, field_schema, f"{path}.{field}"))
    return errors


def _build_schema_validation_result(tool_name: str, errors: list[str]) -> str:
    detail = "; ".join(errors[:10])
    return _build_tool_error_result(tool_name, t(ERR_TOOL_ARGUMENT_SCHEMA_INVALID, tool_name=tool_name, detail=detail))


def prevalidate_tool_round(
    tool_calls: list[Any],
    cfg: ProfileConfig,
    *,
    allow_background_submission: bool = True,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    for tool_call in tool_calls:
        tool_name = tool_call.name
        args = dict(tool_call.arguments or {})
        background_requested = bool(args.pop("run_in_background", False))
        parameters_schema = get_tool_parameters_schema(tool_name, tool_schemas=tool_schemas)
        required_parameters = parameters_schema.get("required", []) if parameters_schema is not None else []
        declared_properties = parameters_schema.get("properties", {}) if parameters_schema is not None else {}
        missing_arguments = sorted(parameter_name for parameter_name in required_parameters if isinstance(parameter_name, str) and parameter_name not in args)
        unsupported_arguments = sorted(argument_name for argument_name in args if parameters_schema is not None and argument_name not in declared_properties)
        if not _is_tool_enabled(tool_name, cfg):
            errors[tool_call.id] = _build_tool_disabled_result(tool_name)
        elif missing_arguments:
            errors[tool_call.id] = _build_missing_required_arguments_result(
                tool_name,
                missing_arguments,
            )
        elif unsupported_arguments:
            errors[tool_call.id] = _build_unsupported_arguments_result(
                tool_name,
                unsupported_arguments,
            )
        elif parameters_schema is not None and (schema_errors := _validate_schema_value(args, parameters_schema, "arguments")):
            errors[tool_call.id] = _build_schema_validation_result(tool_name, schema_errors)
        elif (tool_runs_in_background(tool_name) or background_requested) and not allow_background_submission:
            errors[tool_call.id] = _build_background_task_unsupported_result(tool_name)
    return errors


async def process_single_tool(
    tool_call: Any,
    db: AsyncSession,
    profile: Profile,
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    username: str,
    session_id: str,
    turn: int,
    uid: str,
    allowed_knowledge_base_ids: list[int] | None = None,
    active_tasks: set[asyncio.Task] | None = None,
    context_window_k: int = 4,
    allow_background_submission: bool = True,
    audit_preapproved: bool = False,
) -> InternalMessage:
    tool_name = tool_call.name
    args = dict(tool_call.arguments or {})
    missing_arguments = sorted(parameter_name for parameter_name in get_tool_required_parameters(tool_name) if parameter_name not in args)
    unsupported_arguments = sorted(argument_name for argument_name in args if not tool_schema_has_parameter(tool_name, argument_name))
    background_requested = bool(args.pop("run_in_background", False))
    background_required = tool_runs_in_background(tool_name)
    run_in_background = background_required or background_requested

    LogManager.log_tool_call(turn, tool_name, json.dumps(args, ensure_ascii=False), session_id, uid)

    if not _is_tool_enabled(tool_name, cfg):
        cmd_result = _build_tool_disabled_result(tool_name)
    elif missing_arguments:
        cmd_result = _build_missing_required_arguments_result(tool_name, missing_arguments)
    elif unsupported_arguments:
        cmd_result = _build_unsupported_arguments_result(tool_name, unsupported_arguments)
    elif run_in_background and not allow_background_submission:
        cmd_result = _build_background_task_unsupported_result(tool_name)
    else:
        cmd_result = (
            None
            if audit_preapproved
            else await audit_tool_call(
                db,
                profile,
                cfg,
                tool_name,
                args,
                messages,
                session_id=session_id,
                uid=uid,
            )
        )

    if cmd_result is None and run_in_background:
        from app.core.background_tasks.manager import background_task_manager

        task = await background_task_manager.submit(
            db,
            uid=uid,
            session_id=session_id,
            profile=profile,
            tool_call_id=tool_call.id,
            tool_name=tool_name,
            arguments=args,
            allowed_knowledge_base_ids=allowed_knowledge_base_ids,
            source="llm_tool_call",
            messages=messages,
        )
        cmd_result = _build_background_task_queued_result(tool_name, task.id)

    if cmd_result is None:
        executor_cls = TOOL_EXECUTOR_MAP.get(tool_name)
        if executor_cls:
            instance = executor_cls(
                project_root=os.getcwd(),
                uid=uid,
            )
            # 传递配置给 Executor
            if hasattr(instance, "set_config"):
                instance.set_config(cfg)

            # 传递运行时上下文给 Executor
            if hasattr(instance, "set_runtime_context"):
                dispatch_context = build_dispatch_context(
                    mode="interactive",
                    source="interactive_tool",
                    uid=uid,
                    session_id=session_id,
                    profile=profile,
                    db=db,
                    allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                )
                instance.set_runtime_context(
                    dispatch_context=dispatch_context,
                )

            current_coro = instance.execute(**args)
            task = asyncio.create_task(current_coro)
            if active_tasks is not None:
                active_tasks.add(task)

            start_time = time.perf_counter()
            try:
                cmd_result = await task
            except asyncio.CancelledError:
                duration = time.perf_counter() - start_time

                get_logger("dispatcher").bind(
                    uid=uid,
                    session_id=session_id,
                    tool_name=tool_name,
                    duration=f"{duration:.3f}s",
                ).warning(t("LOG_TOOL_ABORTED", tool_name=tool_name, duration=f"{duration:.3f}s"))

                if not task.done():
                    task.cancel()
                raise
            except Exception as exc:
                cmd_result = _build_tool_error_result(tool_name, format_exception_message(exc))
            finally:
                if active_tasks is not None:
                    active_tasks.discard(task)
        else:
            cmd_result = json.dumps(
                {"error": t(ERR_TOOL_NOT_REGISTERED, tool_name=tool_name)},
                ensure_ascii=False,
            )

    tool_msg = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call.id,
        content=cmd_result,
    )
    truncation_stats = truncate_tool_messages_for_budget(
        tool_msgs=[tool_msg],
        context_window_k=context_window_k,
        budget_tokens=max(1, (context_window_k * 1024) // 2),
        uid=uid,
        session_id=session_id,
    )
    if truncation_stats.truncated_count:
        get_logger("dispatcher").bind(
            uid=uid,
            session_id=session_id,
            tool_name=tool_name,
        ).warning(t("LOG_TOOL_RESULT_TRUNCATED", tool_name=tool_name, context_window_k=context_window_k))

    # 使用原始工具结果记录日志，确保文件日志保留完整数据用于审计；
    # ws/db sink 中的字符级截断仅影响前端推送与数据库存储，不影响文件日志
    LogManager.log_tool_result(turn, cmd_result, session_id, uid)

    return tool_msg
