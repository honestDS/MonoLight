import asyncio
from dataclasses import dataclass
from typing import Any

from app.core.constants import (
    ERR_SESSION_REPLY_PROVIDER_USAGE_METADATA_UPDATE_FAILED,
    ERR_SESSION_REPLY_PROVIDER_USAGE_MISSING,
    ERR_SESSION_REPLY_PROVIDER_USAGE_SESSION_NOT_FOUND,
    ERR_SESSION_REPLY_PROVIDER_USAGE_WORK_ID_INVALID,
)
from app.core.crud.session.reply_provider_usage import session_reply_provider_usage_crud
from app.core.crud.session.session import session_crud
from app.core.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.session_reply_queue.executor_common import _metadata_with_work_order
from app.core.utils.request_token_baseline import (
    PROVIDER_CACHED_TOKENS_METADATA_KEY,
    PROVIDER_INPUT_TOKENS_METADATA_KEY,
    PROVIDER_OUTPUT_TOKENS_METADATA_KEY,
    PROVIDER_REQUEST_ID_METADATA_KEY,
    accumulate_session_cache_metrics,
    build_session_cache_metrics,
    extract_provider_request_usage,
    extract_session_cache_token_totals,
    extract_session_total_output_tokens,
    merge_session_cache_token_totals,
)
from app.models.message import InternalMessage
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.providers.database import AsyncSessionLocal

__all__ = []


@dataclass(frozen=True)
class _ProviderUsagePersistenceResult:
    created: bool
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int


def _without_provider_usage_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            PROVIDER_REQUEST_ID_METADATA_KEY,
            PROVIDER_INPUT_TOKENS_METADATA_KEY,
            PROVIDER_CACHED_TOKENS_METADATA_KEY,
            PROVIDER_OUTPUT_TOKENS_METADATA_KEY,
        }
    }


def _incoming_work_metadata_is_current(
    session,
    work: SessionReplyWorkItem,
    metadata: dict[str, Any],
) -> bool:
    current_work_sequence_no = session.llm_request_metadata_work_sequence_no
    if current_work_sequence_no is None:
        return True
    current_event_sequence_no = session.llm_request_metadata_event_sequence_no or 0
    incoming_event_sequence_no = metadata.get("event_sequence_no", 0)
    return work.sequence_no > current_work_sequence_no or (work.sequence_no == current_work_sequence_no and incoming_event_sequence_no >= current_event_sequence_no)


async def _persist_session_reply_provider_usage(
    metadata: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
) -> _ProviderUsagePersistenceResult | None:
    provider_usage = extract_provider_request_usage(metadata)
    if provider_usage is None:
        return None

    work_id = work.id
    if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0:
        raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_WORK_ID_INVALID))

    request_id, input_tokens, cached_tokens, output_tokens = provider_usage
    async with AsyncSessionLocal() as usage_db:
        _provider_usage, created = await session_reply_provider_usage_crud.create_once(
            usage_db,
            provider_request_id=request_id,
            work_id=work_id,
            session_id=work.session_id,
            uid=work.uid,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
        )
        session = await session_crud.get_by_session_id_for_update(
            usage_db,
            session_id=work.session_id,
            uid=work.uid,
        )
        if session is None:
            raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_SESSION_NOT_FOUND))

        total_input_tokens, total_cached_tokens = extract_session_cache_token_totals(session.llm_request_metadata)
        total_output_tokens = extract_session_total_output_tokens(session.llm_request_metadata)
        if not created:
            return _ProviderUsagePersistenceResult(
                created=False,
                total_input_tokens=total_input_tokens,
                total_cached_tokens=total_cached_tokens,
                total_output_tokens=total_output_tokens,
            )

        new_total_input_tokens = total_input_tokens + input_tokens
        new_total_cached_tokens = total_cached_tokens + cached_tokens
        new_total_output_tokens = total_output_tokens + output_tokens
        incoming_metadata = _metadata_with_work_order(work, _without_provider_usage_fields(metadata))
        if _incoming_work_metadata_is_current(session, work, incoming_metadata):
            metadata_to_persist = incoming_metadata
        else:
            current_metadata = session.llm_request_metadata
            metadata_to_persist = dict(current_metadata) if isinstance(current_metadata, dict) else {}
        metadata_to_persist = {
            **metadata_to_persist,
            **build_session_cache_metrics(new_total_input_tokens, new_total_cached_tokens),
            "total_output_tokens": new_total_output_tokens,
        }

        updated = await session_crud.update_llm_request_metadata(
            usage_db,
            session_id=work.session_id,
            uid=work.uid,
            metadata=metadata_to_persist,
            commit=False,
        )
        if not updated:
            raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_METADATA_UPDATE_FAILED))
        await usage_db.commit()
        return _ProviderUsagePersistenceResult(
            created=True,
            total_input_tokens=new_total_input_tokens,
            total_cached_tokens=new_total_cached_tokens,
            total_output_tokens=new_total_output_tokens,
        )


async def _persist_session_reply_provider_usage_reliably(
    metadata: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
) -> _ProviderUsagePersistenceResult | None:
    operation = asyncio.create_task(_persist_session_reply_provider_usage(metadata, work=work))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
        if not operation.cancelled():
            operation.exception()
        raise


async def _persist_interactive_work_request_metadata(
    metadata: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
) -> None:
    await _persist_session_reply_provider_usage_reliably(metadata, work=work)


async def _generate_reply_with_request_metadata(
    db,
    *,
    work: SessionReplyWorkItem,
    **kwargs: Any,
) -> tuple[InternalMessage, list[InternalMessage], list[dict[str, Any]], dict[str, Any] | None]:
    latest_request_metadata = None
    session_total_output_tokens = 0
    session_total_input_tokens = 0
    session_total_cached_tokens = 0
    work_output_tokens = 0
    if "additional_system_prompt" not in kwargs:
        execution_state = getattr(work, "execution_state", None)
        additional_system_prompt = execution_state.get("additional_system_prompt") if isinstance(execution_state, dict) else None
        if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
            kwargs["additional_system_prompt"] = additional_system_prompt.strip()
    if "guidance_prompt" not in kwargs:
        execution_state = getattr(work, "execution_state", None)
        guidance_prompt = execution_state.get("guidance_prompt") if isinstance(execution_state, dict) else None
        if isinstance(guidance_prompt, str) and guidance_prompt.strip():
            kwargs["guidance_prompt"] = guidance_prompt
    if hasattr(db, "execute"):
        session = await session_crud.get_by_session_id(db, work.session_id)
        if session is not None:
            session_total_output_tokens = extract_session_total_output_tokens(session.llm_request_metadata)
            session_total_input_tokens, session_total_cached_tokens = merge_session_cache_token_totals(session.llm_request_metadata)

    seen_provider_request_ids: set[str] = set()

    async def persist_request_metadata(metadata: dict[str, Any]) -> None:
        nonlocal latest_request_metadata, session_total_output_tokens, session_total_input_tokens, session_total_cached_tokens, work_output_tokens
        provider_usage = extract_provider_request_usage(metadata)
        request_id = provider_usage[0] if provider_usage is not None else None
        request_output_tokens = provider_usage[3] if provider_usage is not None else 0
        first_seen_provider_request = request_id is not None and request_id not in seen_provider_request_ids
        if provider_usage is None or first_seen_provider_request:
            session_total_input_tokens, session_total_cached_tokens = accumulate_session_cache_metrics(
                metadata,
                total_input_tokens=session_total_input_tokens,
                total_cached_tokens=session_total_cached_tokens,
            )
            output_tokens = metadata.get("output_tokens")
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
                session_total_output_tokens += output_tokens
                work_output_tokens += output_tokens
            if request_id is not None:
                seen_provider_request_ids.add(request_id)
        ordered_metadata = _metadata_with_work_order(
            work,
            {
                **metadata,
                "output_tokens": work_output_tokens,
                "total_output_tokens": session_total_output_tokens,
            },
        )
        if provider_usage is not None:
            result = await _persist_session_reply_provider_usage_reliably(ordered_metadata, work=work)
            if result is None:
                raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_MISSING))
            session_total_input_tokens = result.total_input_tokens
            session_total_cached_tokens = result.total_cached_tokens
            session_total_output_tokens = result.total_output_tokens
            if not result.created and first_seen_provider_request:
                work_output_tokens -= request_output_tokens
            ordered_metadata = {
                **ordered_metadata,
                **build_session_cache_metrics(session_total_input_tokens, session_total_cached_tokens),
                "output_tokens": work_output_tokens,
                "total_output_tokens": session_total_output_tokens,
            }
        else:
            await session_crud.update_llm_request_metadata(
                db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=ordered_metadata,
                commit=False,
            )
        latest_request_metadata = _without_provider_usage_fields(ordered_metadata)

    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        **kwargs,
        request_metadata_callback=persist_request_metadata,
    )
    return ai_msg, turn_messages, files, latest_request_metadata
