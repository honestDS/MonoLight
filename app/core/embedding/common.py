"""Shared embedding channel configuration and invocation helpers."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.constants import (
    ERR_EMBEDDING_DATABASE_SESSION_REQUIRED,
    ERR_EMBEDDING_VECTOR_EMPTY,
    ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED,
    ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL,
    ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND,
    ERR_PROFILE_NO_EMBEDDING_MODEL,
)
from app.core.crud.channel.channel import channel_crud
from app.core.i18n import t
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.models.channel import ModelUsage, resolve_model_protocol
from app.providers.embedding import EmbeddingClient


@dataclass(frozen=True, slots=True)
class EmbeddingRuntimeConfig:
    """Immutable embedding runtime settings detached from database entities."""

    channel_id: int
    channel_name: str | None
    model_id: str
    declared_dimensions: int | None
    protocol: str
    timeout: float
    base_url: str
    api_key: str = field(repr=False)
    http_proxy: str | None = field(default=None, repr=False)
    custom_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "custom_headers", MappingProxyType(dict(self.custom_headers)))

    @property
    def embedding_dimensions(self) -> int | None:
        return self.declared_dimensions


def build_embedding_signature(channel_id: int, model_id: str, dimensions: int) -> str:
    canonical = json.dumps(
        {
            "channel_id": channel_id,
            "model_id": model_id,
            "dimensions": dimensions,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def load_embedding_runtime_config(
    db: AsyncSession,
    channel_id: int,
    model_id: str,
    *,
    lock_for_reference_write: bool = False,
    channel_not_found_status_code: int = 400,
    model_not_found_status_code: int = 400,
) -> EmbeddingRuntimeConfig:
    """Load and validate an embedding model without retaining its ORM entity.

    When enabled, locks and reads the latest channel state within a reference-write transaction.
    """
    channel = await channel_crud.lock_for_mutation(db, channel_id=channel_id, commit=False) if lock_for_reference_write else await channel_crud.get(db, channel_id)
    if not channel:
        raise HTTPException(status_code=channel_not_found_status_code, detail=ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND)
    if not channel.is_active:
        raise HTTPException(status_code=400, detail=ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED)
    if not channel.base_url:
        raise HTTPException(status_code=400, detail=ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL)

    model_entry = next(
        (item for item in channel.model_ids or [] if isinstance(item, dict) and item.get("model_id") == model_id and str(item.get("usage")) == ModelUsage.EMBEDDING.value and item.get("is_enabled", True)),
        None,
    )
    if model_entry is None:
        raise HTTPException(status_code=model_not_found_status_code, detail=ERR_PROFILE_NO_EMBEDDING_MODEL)

    model_timeout = model_entry.get("embedding_timeout")
    embedding_timeout = min(float(model_timeout), 600.0) if model_timeout else 30.0
    return EmbeddingRuntimeConfig(
        channel_id=int(channel.id),
        channel_name=channel.name,
        model_id=model_id,
        declared_dimensions=model_entry.get("embedding_dimensions"),
        protocol=resolve_model_protocol(model_entry),
        timeout=embedding_timeout,
        base_url=channel.base_url,
        api_key=channel.get_decrypted_api_key(),
        http_proxy=get_channel_http_proxy(channel),
        custom_headers=get_model_custom_headers(model_entry),
    )


async def embed_texts_with_config(
    config: EmbeddingRuntimeConfig,
    input_texts: list[str],
    batch_size: int = 16,
    dimensions: int | None = None,
    *,
    db: AsyncSession | None = None,
    release_connection: bool = False,
) -> list[list[float]]:
    """Generate embeddings using a detached runtime configuration."""
    if release_connection:
        if db is None:
            raise ValueError(t(ERR_EMBEDDING_DATABASE_SESSION_REQUIRED))
        await db.commit()

    return await EmbeddingClient.embed_texts(
        api_key=config.api_key,
        base_url=config.base_url,
        model_id=config.model_id,
        protocol=config.protocol,
        input_texts=input_texts,
        batch_size=batch_size,
        dimensions=dimensions,
        timeout=config.timeout,
        http_proxy=config.http_proxy,
        custom_headers=dict(config.custom_headers),
    )


async def detect_embedding_dimensions(config: EmbeddingRuntimeConfig) -> int:
    """Probe the model and return the dimension of an actual response vector."""
    embeddings = await embed_texts_with_config(config, ["dimension test"], batch_size=1, dimensions=None)
    if not embeddings or not embeddings[0]:
        raise ValueError(t(ERR_EMBEDDING_VECTOR_EMPTY))
    return len(embeddings[0])


load_embedding_config = load_embedding_runtime_config
embed_texts = embed_texts_with_config
probe_embedding_dimensions = detect_embedding_dimensions


__all__ = [
    "EmbeddingRuntimeConfig",
    "build_embedding_signature",
    "detect_embedding_dimensions",
    "embed_texts",
    "embed_texts_with_config",
    "load_embedding_config",
    "load_embedding_runtime_config",
    "probe_embedding_dimensions",
]
