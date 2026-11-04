import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.constants import (
    ERR_SESSION_REPLY_AUXILIARY_PURPOSE_CONTRIBUTES_TO_TOTALS,
    ERR_SESSION_REPLY_PROVIDER_USAGE_METADATA_UPDATE_FAILED,
    ERR_SESSION_REPLY_PROVIDER_USAGE_MISSING,
    ERR_SESSION_REPLY_PROVIDER_USAGE_SESSION_NOT_FOUND,
    ERR_SESSION_REPLY_PROVIDER_USAGE_WORK_ID_INVALID,
)
from app.core.crud.provider_usage import provider_usage_crud
from app.core.crud.session.session import session_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.provider_usage import cleanup_provider_usage_details
from app.core.session_reply_queue.executor_common import _metadata_with_work_order
from app.core.utils.request_token_baseline import (
    PROVIDER_REQUEST_ID_METADATA_KEY,
    build_session_cache_metrics,
    extract_provider_request_usage,
    extract_session_cache_token_totals,
    extract_session_total_output_tokens,
    merge_session_cache_token_totals,
)
from app.models.message import InternalMessage
from app.models.provider_usage import ProviderUsageDetail, ProviderUsageType
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.providers.database import AsyncSessionLocal

__all__ = []


_PROVIDER_USAGE_OBSERVED_METADATA_KEY = "_provider_usage_observed"
_PROVIDER_CHANNEL_KEY_METADATA_KEY = "_provider_channel_key"
_PROVIDER_CHANNEL_NAME_METADATA_KEY = "_provider_channel_name"
_PROVIDER_RESPONSE_MODEL_ID_METADATA_KEY = "_provider_response_model_id"
_PROVIDER_IMAGE_COUNT_METADATA_KEY = "_provider_image_count"


@dataclass(frozen=True)
class _ProviderUsagePersistenceResult:
    created: bool
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int


def _without_provider_usage_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    filtered_metadata: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.startswith("_provider_"):
            continue
        filtered_metadata[key] = value
    return filtered_metadata


def _validated_provider_request_id(metadata: dict[str, Any]) -> str | None:
    request_id = metadata.get(PROVIDER_REQUEST_ID_METADATA_KEY)
    request_id = request_id.strip() if isinstance(request_id, str) else ""
    if not request_id or len(request_id) > 64:
        return None
    return request_id


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
    request_purpose: ProviderUsageType,
    update_session_context: bool = True,
) -> _ProviderUsagePersistenceResult | None:
    provider_usage = extract_provider_request_usage(metadata)
    has_token_usage = provider_usage is not None
    if provider_usage is None:
        if metadata.get(_PROVIDER_USAGE_OBSERVED_METADATA_KEY) is not True:
            return None
        request_id = _validated_provider_request_id(metadata)
        if request_id is None:
            return None
        input_tokens = None
        cached_tokens = None
        output_tokens = None
    else:
        request_id, input_tokens, cached_tokens, output_tokens = provider_usage

    work_id = work.id
    if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0:
        raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_WORK_ID_INVALID))

    protocol = metadata.get("protocol")
    protocol = protocol.lower() if isinstance(protocol, str) else None
    token_usage = metadata.get("_provider_token_usage")
    token_usage = token_usage if isinstance(token_usage, dict) else {}
    detail = ProviderUsageDetail(
        provider_request_id=request_id,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        type=request_purpose.value,
        channel_key=metadata.get(_PROVIDER_CHANNEL_KEY_METADATA_KEY),
        channel_id=metadata.get("channel_id"),
        channel_name=metadata.get(_PROVIDER_CHANNEL_NAME_METADATA_KEY),
        model_id=metadata.get("model_id"),
        response_model_id=metadata.get(_PROVIDER_RESPONSE_MODEL_ID_METADATA_KEY),
        protocol=protocol,
        uid=work.uid,
        session_id=work.session_id,
        work_id=work_id,
        input_tokens=token_usage.get("input_tokens"),
        cached_tokens=token_usage.get("cached_tokens"),
        output_tokens=token_usage.get("output_tokens"),
        image_count=metadata.get(_PROVIDER_IMAGE_COUNT_METADATA_KEY),
    )
    async with AsyncSessionLocal() as usage_db:
        created = await provider_usage_crud.create_once(usage_db, detail)
        if request_purpose is ProviderUsageType.SCHEDULED_TASK:
            await usage_db.commit()
            if created:
                await cleanup_provider_usage_details(usage_db)
            return None
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

        new_total_input_tokens = total_input_tokens
        new_total_cached_tokens = total_cached_tokens
        new_total_output_tokens = total_output_tokens
        if request_purpose is ProviderUsageType.MAIN_DIALOGUE and has_token_usage:
            new_total_input_tokens += input_tokens
            new_total_cached_tokens += cached_tokens
            new_total_output_tokens += output_tokens

            should_update_session_context = update_session_context
            if should_update_session_context and metadata.get("input_tokens_source") == "estimated" and input_tokens == 0 and output_tokens > 0:
                should_update_session_context = False

            incoming_metadata = _metadata_with_work_order(work, _without_provider_usage_fields(metadata))
            if not should_update_session_context:
                metadata_to_persist = incoming_metadata
            elif _incoming_work_metadata_is_current(session, work, incoming_metadata):
                metadata_to_persist = incoming_metadata
            else:
                current_metadata = session.llm_request_metadata
                metadata_to_persist = _without_provider_usage_fields(current_metadata) if isinstance(current_metadata, dict) else {}
            metadata_to_persist = {
                **metadata_to_persist,
                **build_session_cache_metrics(
                    new_total_input_tokens,
                    new_total_cached_tokens,
                ),
                "total_output_tokens": new_total_output_tokens,
            }

            updated = await session_crud.update_llm_request_metadata(
                usage_db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=metadata_to_persist,
                commit=False,
                update_context=should_update_session_context,
            )
            if not updated:
                raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_METADATA_UPDATE_FAILED))
        await usage_db.commit()
        await cleanup_provider_usage_details(usage_db)
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
    request_purpose: ProviderUsageType,
    update_session_context: bool = True,
) -> _ProviderUsagePersistenceResult | None:
    operation = asyncio.create_task(
        _persist_session_reply_provider_usage(
            metadata,
            work=work,
            request_purpose=request_purpose,
            update_session_context=update_session_context,
        )
    )
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
    request_purpose: ProviderUsageType = ProviderUsageType.MAIN_DIALOGUE,
    update_session_context: bool = True,
) -> _ProviderUsagePersistenceResult | None:
    return await _persist_session_reply_provider_usage_reliably(
        metadata,
        work=work,
        request_purpose=request_purpose,
        update_session_context=update_session_context,
    )


async def _generate_auxiliary_reply_with_request_metadata(
    db,
    *,
    work: SessionReplyWorkItem,
    request_purpose: ProviderUsageType,
    **kwargs: Any,
) -> tuple[InternalMessage, list[InternalMessage], list[dict[str, Any]], dict[str, Any] | None]:
    """Generate an auxiliary reply with an explicit provider-request purpose."""
    if request_purpose.contributes_to_session_totals:
        raise ValueError(t(ERR_SESSION_REPLY_AUXILIARY_PURPOSE_CONTRIBUTES_TO_TOTALS))
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
        nonlocal latest_request_metadata, session_total_output_tokens, session_total_input_tokens, session_total_cached_tokens
        nonlocal work_output_tokens
        metadata = {
            **metadata,
            "request_purpose": request_purpose.value,
        }
        provider_usage = extract_provider_request_usage(metadata)
        observed_request_id = _validated_provider_request_id(metadata) if metadata.get(_PROVIDER_USAGE_OBSERVED_METADATA_KEY) is True else None
        request_id = provider_usage[0] if provider_usage is not None else None
        request_output_tokens = provider_usage[3] if provider_usage is not None else 0
        first_seen_provider_request = request_id is not None and request_id not in seen_provider_request_ids
        if provider_usage is None or first_seen_provider_request:
            output_tokens = metadata.get("output_tokens")
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
                work_output_tokens += output_tokens
            if request_id is not None:
                seen_provider_request_ids.add(request_id)
        ordered_metadata = _metadata_with_work_order(
            work,
            {
                **metadata,
                **build_session_cache_metrics(
                    session_total_input_tokens,
                    session_total_cached_tokens,
                ),
                "output_tokens": work_output_tokens,
                "total_output_tokens": session_total_output_tokens,
            },
        )
        if provider_usage is not None or observed_request_id is not None:
            result = await _persist_session_reply_provider_usage_reliably(
                ordered_metadata,
                work=work,
                request_purpose=request_purpose,
            )
            if result is None:
                raise RuntimeError(t(ERR_SESSION_REPLY_PROVIDER_USAGE_MISSING))
            session_total_input_tokens = result.total_input_tokens
            session_total_cached_tokens = result.total_cached_tokens
            session_total_output_tokens = result.total_output_tokens
            if not result.created and first_seen_provider_request:
                work_output_tokens -= request_output_tokens
            ordered_metadata = {
                **ordered_metadata,
                **build_session_cache_metrics(
                    session_total_input_tokens,
                    session_total_cached_tokens,
                ),
                "output_tokens": work_output_tokens,
                "total_output_tokens": session_total_output_tokens,
            }
        latest_request_metadata = _without_provider_usage_fields(ordered_metadata)

    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        **kwargs,
        request_metadata_callback=persist_request_metadata,
    )
    return ai_msg, turn_messages, files, latest_request_metadata
