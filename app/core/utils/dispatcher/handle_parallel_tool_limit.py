import json

from app.core.prompts import (
    ERR_PARALLEL_LIMIT_EXCEEDED,
)
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
)


def handle_parallel_tool_limit(
    tool_calls: list[InternalToolCall],
    max_parallel_tools: int,
) -> tuple[list[InternalToolCall], list[InternalMessage]]:
    requested = len(tool_calls)
    if requested <= max_parallel_tools:
        return list(tool_calls), []

    executable_tool_calls = list(tool_calls[:max_parallel_tools])
    rejected_tool_results = [
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps(
                {
                    "status": "failed",
                    "tool_name": tool_call.name,
                    "error": "parallel_limit_exceeded",
                    "requested": requested,
                    "limit": max_parallel_tools,
                    "executed": False,
                    "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(
                        requested=requested,
                        limit=max_parallel_tools,
                    ),
                },
                ensure_ascii=False,
            ),
        )
        for tool_call in tool_calls[max_parallel_tools:]
    ]
    return executable_tool_calls, rejected_tool_results
