from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED,
)
from app.core.context import ContextManager
from app.core.exceptions import LLMException
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.context_summary.cleanup import cleanup_context_summary_work_safely
from app.core.utils.context_summary.common import (
    ContextSummaryWorkInvalidError,
    ContextSummaryWorkValidityChecker,
)
from app.core.utils.context_summary.engine import refine_context_summary_to_fit
from app.core.utils.context_summary.reduction import refine_completed_summary_stage
from app.core.utils.context_summary.service import get_context_summary_state
from app.core.utils.context_summary.snapshot import build_frozen_context_summary_snapshot
from app.core.utils.context_summary.stage import (
    build_summary_work_identity,
    generate_snapshot_summary_result,
)
from app.core.utils.dispatcher.markdown_instruction import (
    inherit_user_request_context,
    materialize_user_environment_prompts,
)
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalConversationSummary, InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.providers.llm.client import estimate_request_context_tokens


def serialize_frozen_context_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    serialized: list[dict[str, Any]] = []
    for item in messages:
        message = item.model_copy(deep=True) if isinstance(item, InternalMessage) else InternalMessage.model_validate(item)
        if message.role == MessageRole.SYSTEM:
            continue
        serialized.append(message.model_dump(mode="json", exclude_none=True))
    return serialized


def load_frozen_context_messages(messages: Any) -> list[InternalMessage] | None:
    if not isinstance(messages, list):
        return None
    return [InternalMessage.model_validate(item) for item in serialize_frozen_context_messages(messages)]


def _select_frozen_compaction_segment(
    messages: list[InternalMessage],
    *,
    pending_multimodal_tool_call_ids: set[str] | None = None,
) -> list[InternalMessage]:
    if not messages:
        return []

    safe_end = 1 if is_context_summary_message(messages[0]) else 0
    protected_user_index = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == MessageRole.USER:
            if isinstance(message.content, list) or message.attachments:
                protected_user_index = index
            break

    index = 0
    while index < len(messages):
        if index == protected_user_index:
            break
        message = messages[index]
        if message.role == MessageRole.TOOL:
            break
        if message.role != MessageRole.ASSISTANT:
            index += 1
            continue

        tool_calls = message.tool_calls or []
        if pending_multimodal_tool_call_ids and any(tool_call.id in pending_multimodal_tool_call_ids for tool_call in tool_calls):
            break
        if not tool_calls:
            safe_end = index + 1
            index += 1
            continue

        required_ids = {tool_call.id for tool_call in tool_calls}
        found_ids: set[str] = set()
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == MessageRole.TOOL:
            tool_message = messages[cursor]
            if tool_message.tool_call_id in required_ids:
                found_ids.add(tool_message.tool_call_id)
            cursor += 1
        if not required_ids.issubset(found_ids):
            break
        safe_end = cursor
        index = cursor

    if safe_end <= 0:
        return []
    return messages[:safe_end]


def _covered_user_from_segment(messages: list[InternalMessage]) -> tuple[int | None, str | None]:
    covered_message_id = None
    covered_content = None
    for message in messages:
        if message.role != MessageRole.USER:
            continue
        if message.conversation_summary is not None:
            covered_message_id = message.conversation_summary.covered_user_message_id
            covered_content = message.content if isinstance(message.content, str) else None
            continue
        if not isinstance(message.content, str):
            continue
        if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id > 0:
            covered_message_id = message.id
            covered_content = message.content
    return covered_message_id, covered_content


async def compact_frozen_context_messages(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    context_window_k: int,
    max_tokens: int,
    tools: list[dict] | None,
    model_id: str | None = None,
    protocol: str | None = None,
    channel_id: int | None = None,
    work_id: int | None = None,
    measure_request_tokens: Callable[[list[InternalMessage]], int] | None = None,
    safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    force: bool = False,
    pending_multimodal_tool_call_ids: set[str] | None = None,
) -> list[InternalMessage]:
    system_messages = [message for message in messages if message.role == MessageRole.SYSTEM]
    history_messages = [message for message in messages if message.role != MessageRole.SYSTEM]
    threshold_percent = cfg.other.context_summary_threshold_percent
    compression_target_tokens = calculate_context_summary_trigger_tokens(
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        threshold_percent=threshold_percent,
        safety_margin_tokens=safety_margin_tokens,
    )

    def estimate_request_tokens(request_messages: list[InternalMessage]) -> int:
        return estimate_request_context_tokens(
            materialize_user_environment_prompts(request_messages),
            tools,
            model_id=model_id,
            protocol=protocol,
            channel_id=channel_id,
        )

    request_measure = measure_request_tokens or estimate_request_tokens
    current_request_tokens = request_measure(messages)
    if not force and current_request_tokens <= compression_target_tokens:
        return messages

    if force:
        compression_target_tokens = min(compression_target_tokens, current_request_tokens - 1)

    segment = _select_frozen_compaction_segment(
        history_messages,
        pending_multimodal_tool_call_ids=pending_multimodal_tool_call_ids,
    )
    if not segment:
        return messages

    covered_message_id, covered_content = _covered_user_from_segment(segment)
    summary_through_id = covered_message_id or 0
    for message in segment:
        if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id > 0:
            summary_through_id = max(summary_through_id, message.id)
        if message.conversation_summary is not None:
            summary_through_id = max(summary_through_id, message.conversation_summary.through_message_id)
    snapshot = build_frozen_context_summary_snapshot(segment, work_id=work_id)
    _, work_dedupe_key, _ = build_summary_work_identity(
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        revision=0,
    )
    replacement_template = InternalMessage(role=MessageRole.USER)
    inherit_user_request_context(replacement_template, segment)

    def build_candidate_messages(summary: str) -> list[InternalMessage]:
        replacement = replacement_template.model_copy(
            deep=True,
            update={
                "content": covered_content,
                "conversation_summary": InternalConversationSummary(
                    through_message_id=summary_through_id,
                    content=summary,
                    covered_user_message_id=covered_message_id,
                ),
            },
        )
        return [
            *system_messages,
            replacement,
            *history_messages[len(segment) :],
        ]

    def measure_candidate_request_tokens(summary: str) -> int:
        return request_measure(build_candidate_messages(summary))

    try:
        generated = await generate_snapshot_summary_result(
            db,
            session_id=session_id,
            uid=uid,
            profile=profile,
            cfg=cfg,
            snapshot=snapshot,
            existing_summary=None,
            existing_summary_revision=0,
            safety_margin_tokens=safety_margin_tokens,
            work_validity_checker=work_validity_checker,
        )
        candidate_summary = generated.content
        generated_stage = generated.completed_stage
        if not isinstance(candidate_summary, str) or not candidate_summary.strip() or generated_stage is None:
            raise LLMException(message=ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED)
        completed_stage = generated_stage

        async def refine_candidate(summary: str, refinement_index: int) -> str:
            nonlocal completed_stage
            refined = await refine_completed_summary_stage(
                db,
                profile=profile,
                cfg=cfg,
                lower_stage=completed_stage,
                safety_margin_tokens=safety_margin_tokens,
                refinement_index=refinement_index,
                work_validity_checker=work_validity_checker,
            )
            completed_stage = refined.stage
            return refined.content

        candidate_summary = await refine_context_summary_to_fit(
            candidate_summary,
            input_budget_tokens=compression_target_tokens,
            measure_request_tokens=measure_candidate_request_tokens,
            refine_summary=refine_candidate,
            model_id=model_id,
            protocol=protocol,
        )
        return build_candidate_messages(candidate_summary)
    except ContextSummaryWorkInvalidError:
        raise
    except RuntimeError as exc:
        raise LLMException(message=ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED) from exc
    finally:
        await cleanup_context_summary_work_safely(work_dedupe_key)


async def capture_frozen_context_messages(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    before_id: int,
) -> list[dict[str, Any]]:
    if not isinstance(before_id, int) or isinstance(before_id, bool) or before_id <= 0:
        raise ValueError("before_id must be a positive integer")

    summary_state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    use_summary = summary_state.message_id is None or summary_state.message_id < before_id
    after_id = summary_state.message_id if use_summary else None
    raw_history = await ContextManager._load_history_backward_by_id(
        db,
        session_id=session_id,
        uid=uid,
        before_id=before_id,
        after_id=after_id,
    )
    raw_history = [message for message in raw_history if message.is_processed]
    history = ContextManager.audit_tool_chain(
        list(reversed(parse_db_messages_to_internal(raw_history))),
        uid=uid,
        session_id=session_id,
        emit_logs=False,
    )
    messages: list[InternalMessage] = []
    if use_summary:
        summary_message = summary_state.as_message()
        if summary_message is not None:
            messages.append(summary_message)
    messages.extend(history)
    return serialize_frozen_context_messages(messages)
