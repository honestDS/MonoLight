from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.provider_usage import provider_usage_crud
from app.core.log import get_logger
from app.models.message import InternalResponse
from app.models.provider_usage import ProviderUsageDetail, ProviderUsageType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderUsageSource:
    channel_id: int | None = None
    channel_key: str | None = None
    channel_name: str | None = None
    model_id: str | None = None
    protocol: str | None = None

    @classmethod
    def from_channel(cls, channel: Any, *, model_id: str, protocol: str) -> "ProviderUsageSource":
        return cls(
            channel_id=channel.id,
            channel_key=channel.usage_key,
            channel_name=channel.name,
            model_id=model_id,
            protocol=protocol.lower(),
        )


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _response_image_count(response: InternalResponse) -> int | None:
    if not response.message.generated_images:
        return None
    return len(response.message.generated_images)


def _response_token_usage(response: InternalResponse) -> tuple[int | None, int | None, int | None]:
    usage = response.usage if isinstance(response.usage, Mapping) else {}
    input_tokens = _non_negative_int(usage.get("prompt_tokens"))
    cached_tokens = _non_negative_int(usage.get("cached_tokens"))
    output_tokens = _non_negative_int(usage.get("completion_tokens"))

    if input_tokens is not None and cached_tokens is not None:
        cached_tokens = min(cached_tokens, input_tokens)
    return input_tokens, cached_tokens, output_tokens


async def record_provider_response_usage(
    response: InternalResponse,
    *,
    usage_type: ProviderUsageType,
    source: ProviderUsageSource,
    uid: str | None = None,
    session_id: str | None = None,
) -> None:
    input_tokens, cached_tokens, output_tokens = _response_token_usage(response)
    await record_provider_usage(
        usage_type=usage_type,
        source=source,
        uid=uid,
        session_id=session_id,
        response_model_id=response.model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        image_count=_response_image_count(response),
    )


async def record_provider_usage(
    *,
    usage_type: ProviderUsageType,
    source: ProviderUsageSource,
    uid: str | None = None,
    session_id: str | None = None,
    response_model_id: str | None = None,
    input_tokens: int | None = None,
    cached_tokens: int | None = None,
    output_tokens: int | None = None,
    image_count: int | None = None,
    rerank_search_units: int | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            detail = ProviderUsageDetail(
                provider_request_id=uuid4().hex,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                type=usage_type.value,
                channel_key=source.channel_key,
                channel_id=source.channel_id,
                channel_name=source.channel_name,
                model_id=source.model_id,
                response_model_id=response_model_id,
                protocol=source.protocol,
                uid=uid,
                session_id=session_id,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                image_count=image_count,
                rerank_search_units=rerank_search_units,
            )
            created = await provider_usage_crud.create_once(db, detail)
            await db.commit()
            if created:
                await cleanup_provider_usage_details(db)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("Provider usage recording failed")


async def cleanup_provider_usage_details(db: AsyncSession) -> None:
    try:
        await provider_usage_crud.cleanup_if_due(db, now=datetime.now(UTC).replace(tzinfo=None))
        await db.commit()
    except Exception as exc:
        try:
            await db.rollback()
        except Exception as rollback_exc:
            logger.bind(exception_type=type(rollback_exc).__name__).warning("Provider usage cleanup rollback failed")
        logger.bind(exception_type=type(exc).__name__).warning("Provider usage cleanup failed")


def build_reply_usage_snapshot(channel: Any, response: InternalResponse) -> dict[str, Any]:
    input_tokens, cached_tokens, output_tokens = _response_token_usage(response)
    return {
        "_provider_channel_key": channel.usage_key,
        "_provider_channel_name": channel.name,
        "_provider_response_model_id": response.model,
        "_provider_usage_observed": True,
        "_provider_image_count": _response_image_count(response),
        "_provider_token_usage": {
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
        },
    }


__all__ = [
    "ProviderUsageSource",
    "build_reply_usage_snapshot",
    "cleanup_provider_usage_details",
    "record_provider_response_usage",
    "record_provider_usage",
]
