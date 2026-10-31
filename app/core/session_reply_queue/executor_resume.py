import hashlib
import json
from collections import Counter, deque
from typing import Any

from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.session_reply_queue.executor_interactive import _save_interactive_work_execution_checkpoint
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.session_todo_snapshot import persist_session_todo_snapshot_on_tool_results
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkType

__all__ = ["restore_interactive_tool_replies"]


async def restore_interactive_tool_replies(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]

    def assistant_identity(message: InternalMessage, from_end: int) -> tuple[str, int]:
        if message.id is not None:
            return ("message_id", message.id)
        return ("assistant_from_end", from_end)

    assistant_indexes = [index for index, message in enumerate(messages) if message.role == MessageRole.ASSISTANT]
    assistant_from_end = {index: ordinal for ordinal, index in enumerate(reversed(assistant_indexes), start=1)}
    groups: list[dict[str, Any]] = []
    checkpoint_result_row_ids: set[int] = set()
    current_group = None
    for index, message in enumerate(messages):
        if message.role == MessageRole.ASSISTANT:
            current_group = {
                "identity": assistant_identity(message, assistant_from_end[index]),
                "assistant": message,
                "existing_counts": Counter(),
                "existing_without_id_counts": Counter(),
                "missing": [],
            }
            groups.append(current_group)
        elif current_group is not None and message.role == MessageRole.TOOL:
            if message.id is not None:
                checkpoint_result_row_ids.add(message.id)
            if message.tool_call_id is not None:
                current_group["existing_counts"][message.tool_call_id] += 1
                if message.id is None:
                    current_group["existing_without_id_counts"][message.tool_call_id] += 1

    for group in groups:
        remaining_existing = Counter(group["existing_counts"])
        for call_index, tool_call in enumerate(group["assistant"].tool_calls or []):
            if remaining_existing[tool_call.id] > 0:
                remaining_existing[tool_call.id] -= 1
                continue
            group["missing"].append((call_index, tool_call))

    if not any(group["missing"] for group in groups):
        return checkpoint

    await ensure_session_reply_work_claim(db)
    restored_results: dict[tuple[tuple[str, int], int], InternalMessage] = {}
    history_kwargs = {"work_id": work.id} if work.work_type == SessionReplyWorkType.AGENT_WORK else {}
    for group in groups:
        assistant = group["assistant"]
        if not group["missing"] or assistant.id is None:
            continue

        pending_by_id: dict[str, deque[int]] = {}
        for call_index, tool_call in group["missing"]:
            pending_by_id.setdefault(tool_call.id, deque()).append(call_index)
        no_id_results_to_skip = Counter(group["existing_without_id_counts"])
        cursor = None
        while any(pending_by_id.values()):
            page = await message_crud.get_history_forward_by_id(
                db,
                session_id=work.session_id,
                uid=work.uid,
                after_id=assistant.id,
                page_after_id=cursor,
                limit=200,
                **history_kwargs,
            )
            if not page:
                break

            tool_result_rows = []
            reached_next_assistant = False
            # 只在当前assistant区间内恢复，遇到下一条assistant即停止。
            for row in page:
                if row.role == MessageRole.ASSISTANT:
                    reached_next_assistant = True
                    break
                if row.role == MessageRole.TOOL and row.type == MessageType.TOOL_RESULT:
                    tool_result_rows.append(row)

            rows_by_id = {row.id: row for row in tool_result_rows}
            for result in parse_db_messages_to_internal(tool_result_rows):
                row = rows_by_id.get(result.id)
                if row is None or row.id in checkpoint_result_row_ids:
                    continue
                call_id = result.tool_call_id
                if no_id_results_to_skip[call_id] > 0:
                    no_id_results_to_skip[call_id] -= 1
                    continue
                pending = pending_by_id.get(call_id)
                if not pending:
                    continue
                call_index = pending.popleft()
                result.created_at = row.created_at.timestamp()
                restored_results[(group["identity"], call_index)] = result

            if reached_next_assistant:
                break
            cursor = page[-1].id

    # 全部区间扫描完成后再生成未知结果，避免后续区间误认新写入行。
    newly_created_tool_results: list[InternalMessage] = []
    for group in groups:
        for call_index, tool_call in group["missing"]:
            result_key = (group["identity"], call_index)
            if result_key in restored_results:
                continue
            tool_result = InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                tool_execution_status="execution_unknown",
                content=json.dumps(
                    {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED},
                    ensure_ascii=False,
                ),
            )
            dedupe_payload = [
                _result_message_dedupe_key(work),
                group["identity"],
                call_index,
                tool_call.id,
            ]
            saved = await save_message(
                db,
                work.session_id,
                work.uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                tool_result,
                work.profile_id,
                is_processed=True,
                commit=False,
                dedupe_key=hashlib.sha256(json.dumps(dedupe_payload, ensure_ascii=False).encode()).hexdigest(),
            )
            tool_result.id = saved.id
            tool_result.created_at = saved.created_at
            restored_results[result_key] = tool_result
            newly_created_tool_results.append(tool_result)

    await persist_session_todo_snapshot_on_tool_results(
        db,
        uid=work.uid,
        session_id=work.session_id,
        tool_results=newly_created_tool_results,
        commit=False,
    )

    def insert_results(raw_messages: list[Any]) -> list[Any]:
        parsed_messages = [InternalMessage.model_validate(message) for message in raw_messages]
        assistant_indexes = [index for index, message in enumerate(parsed_messages) if message.role == MessageRole.ASSISTANT]
        assistant_from_end = {index: ordinal for ordinal, index in enumerate(reversed(assistant_indexes), start=1)}
        output = []
        index = 0
        while index < len(parsed_messages):
            message = parsed_messages[index]
            output.append(raw_messages[index])
            index += 1
            if message.role != MessageRole.ASSISTANT:
                continue
            identity = assistant_identity(message, assistant_from_end[index - 1])
            while index < len(parsed_messages) and parsed_messages[index].role == MessageRole.TOOL:
                output.append(raw_messages[index])
                index += 1
            for call_index, _ in enumerate(message.tool_calls or []):
                result = restored_results.get((identity, call_index))
                if result is not None:
                    output.append(result.model_dump(mode="json"))
        return output

    new_checkpoint = {
        **checkpoint,
        "messages": insert_results(checkpoint["messages"]),
    }
    turn_messages = checkpoint.get("turn_messages")
    if isinstance(turn_messages, list):
        new_checkpoint["turn_messages"] = insert_results(turn_messages)
    await _save_interactive_work_execution_checkpoint(
        db,
        new_checkpoint.copy(),
        work=work,
        worker_id=worker_id,
    )
    return new_checkpoint
