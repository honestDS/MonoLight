from collections.abc import Iterator
from dataclasses import dataclass

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_CHUNK_METADATA_OVER_BUDGET,
    ERR_CONTEXT_SUMMARY_CHUNK_OVER_BUDGET,
    ERR_CONTEXT_SUMMARY_MESSAGE_ID_REQUIRED,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t
from app.core.utils.context_summary.common import serialize_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage


@dataclass(frozen=True)
class SummarySourceUnit:
    message_start_id: int
    message_end_id: int
    token_count: int
    content: str


def _message_chunk_content(
    *,
    message: InternalMessage,
    part_index: int,
    payload: str,
) -> str:
    if message.id is None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MESSAGE_ID_REQUIRED))
    return f'<message_chunk message_id="{message.id}" role="{message.role.value}" part="{part_index}">\n{payload}\n</message_chunk>'


def _largest_fitting_prefix(
    text: str,
    *,
    content_builder,
    max_tokens: int,
) -> int:
    low = 1
    high = len(text)
    best = 0
    while low <= high:
        middle = (low + high) // 2
        if estimate_tokens(content_builder(text[:middle])) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def split_oversized_message(
    message: InternalMessage,
    *,
    max_unit_tokens: int,
) -> Iterator[SummarySourceUnit]:
    if max_unit_tokens <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_unit_tokens"))
    if message.id is None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MESSAGE_ID_REQUIRED))

    serialized = serialize_message(message)
    serialized_tokens = max(1, estimate_tokens(serialized))
    if serialized_tokens <= max_unit_tokens:
        yield SummarySourceUnit(
            message_start_id=message.id,
            message_end_id=message.id,
            token_count=serialized_tokens,
            content=serialized,
        )
        return

    remaining = serialized
    part_index = 0
    while remaining:

        def content_builder(payload: str) -> str:
            return _message_chunk_content(
                message=message,
                part_index=part_index,
                payload=payload,
            )

        prefix_length = _largest_fitting_prefix(
            remaining,
            content_builder=content_builder,
            max_tokens=max_unit_tokens,
        )
        if prefix_length <= 0:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_CHUNK_METADATA_OVER_BUDGET))
        content = content_builder(remaining[:prefix_length])
        token_count = max(1, estimate_tokens(content))
        if token_count > max_unit_tokens:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_CHUNK_OVER_BUDGET))
        yield SummarySourceUnit(
            message_start_id=message.id,
            message_end_id=message.id,
            token_count=token_count,
            content=content,
        )
        remaining = remaining[prefix_length:]
        part_index += 1


def iter_round_source_units(
    messages: list[InternalMessage],
    *,
    max_unit_tokens: int,
) -> Iterator[SummarySourceUnit]:
    if max_unit_tokens <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_unit_tokens"))

    for message in messages:
        yield from split_oversized_message(
            message,
            max_unit_tokens=max_unit_tokens,
        )
