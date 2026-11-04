import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from loguru import logger
from sqlalchemy import text
from sqlmodel import select

import app.core.provider_usage as provider_usage_module
from app.core.crud.provider_usage import provider_usage_crud
from app.core.provider_usage import ProviderUsageSource
from app.models.channel import ModelChannel
from app.models.provider_usage import ProviderUsageDaily, ProviderUsageDetail, ProviderUsageType
from app.models.system_setting import SystemSetting
from app.providers.embedding.client import EmbeddingClient
from app.providers.image_generation.client import ImageGenerationClient
from app.providers.rerank.client import RerankClient
from app.transformers.cohere_rerank import CohereRerankTransformer
from app.transformers.openai import OpenAIEmbeddingTransformer, OpenAIImageGenerationTransformer
from app.transformers.openai import image_generation as image_generation_module

_PROVIDER_USAGE_TABLES = (
    ModelChannel.__table__,
    ProviderUsageDetail.__table__,
    ProviderUsageDaily.__table__,
    SystemSetting.__table__,
)


def _channel(name: str) -> ModelChannel:
    return ModelChannel(
        name=name,
        api_key="enc:v1:provider-usage-test-key",
        base_url="https://provider.invalid/v1",
        model_ids=[
            {
                "model_id": "chat-model",
                "usage": "CHAT",
                "protocol": "OPENAI",
                "context_window_k": 32,
                "max_tokens": 512,
            },
            {
                "model_id": "image-model",
                "usage": "IMAGE_GENERATION",
                "protocol": "OPENAI_IMAGE",
            },
        ],
    )


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _detail(
    *,
    provider_request_id: str,
    created_at: datetime,
    channel: ModelChannel,
    usage_type: ProviderUsageType,
    model_id: str,
    protocol: str,
    response_model_id: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    image_count: int = 0,
) -> ProviderUsageDetail:
    assert channel.id is not None
    return ProviderUsageDetail(
        provider_request_id=provider_request_id,
        created_at=created_at,
        type=usage_type.value,
        channel_key=channel.usage_key,
        channel_id=channel.id,
        channel_name=channel.name,
        model_id=model_id,
        response_model_id=response_model_id,
        protocol=protocol,
        uid="provider-usage-user",
        session_id="provider-usage-session",
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        image_count=image_count,
    )


@pytest.mark.asyncio
async def test_provider_usage_storage_deduplicates_requests_and_preserves_channel_snapshots(database_factory) -> None:
    async with database_factory(
        tables=_PROVIDER_USAGE_TABLES,
        foreign_keys=True,
        name="provider-usage-storage-workflow.sqlite3",
    ) as factory:
        original_channel = _channel("provider-usage-channel")
        async with factory() as db:
            assert (await db.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            db.add(original_channel)
            await db.commit()

            original_id = original_channel.id
            original_key = original_channel.usage_key
            original_name = original_channel.name
            actual_model_ids = {item["model_id"] for item in original_channel.model_ids}
            assert actual_model_ids == {"chat-model", "image-model"}

            chat_detail = _detail(
                provider_request_id="provider-request-chat",
                created_at=_utc_naive(datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)),
                channel=original_channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=120,
                cached_tokens=30,
                output_tokens=9,
            )
            image_detail = _detail(
                provider_request_id="provider-request-image",
                created_at=_utc_naive(datetime(2026, 1, 2, 0, 0, 1, tzinfo=UTC)),
                channel=original_channel,
                usage_type=ProviderUsageType.IMAGE_GENERATION,
                model_id="image-model",
                protocol="openai_image",
                response_model_id="image-response-model",
                input_tokens=8,
                cached_tokens=2,
                output_tokens=1,
                image_count=2,
            )
            assert chat_detail.model_id in actual_model_ids
            assert image_detail.model_id in actual_model_ids
            assert await provider_usage_crud.create_once(db, chat_detail) is True
            assert await provider_usage_crud.create_once(db, image_detail) is True
            assert (
                await provider_usage_crud.create_once(
                    db,
                    _detail(
                        provider_request_id="provider-request-chat",
                        created_at=chat_detail.created_at,
                        channel=original_channel,
                        usage_type=ProviderUsageType.MAIN_DIALOGUE,
                        model_id="chat-model",
                        protocol="openai",
                        response_model_id="chat-response-model",
                        input_tokens=120,
                        cached_tokens=30,
                        output_tokens=9,
                    ),
                )
                is False
            )
            await db.commit()

        async with factory() as db:
            details = list((await db.execute(select(ProviderUsageDetail).order_by(ProviderUsageDetail.provider_request_id))).scalars().all())
            assert [detail.provider_request_id for detail in details] == ["provider-request-chat", "provider-request-image"]
            detail_by_request = {detail.provider_request_id: detail for detail in details}
            assert (
                detail_by_request["provider-request-chat"].channel_key,
                detail_by_request["provider-request-chat"].channel_id,
                detail_by_request["provider-request-chat"].channel_name,
                detail_by_request["provider-request-chat"].model_id,
                detail_by_request["provider-request-chat"].type,
                detail_by_request["provider-request-chat"].input_tokens,
                detail_by_request["provider-request-chat"].cached_tokens,
                detail_by_request["provider-request-chat"].output_tokens,
            ) == (
                original_key,
                original_id,
                original_name,
                "chat-model",
                ProviderUsageType.MAIN_DIALOGUE.value,
                120,
                30,
                9,
            )
            assert (
                detail_by_request["provider-request-image"].channel_key,
                detail_by_request["provider-request-image"].channel_id,
                detail_by_request["provider-request-image"].channel_name,
                detail_by_request["provider-request-image"].model_id,
                detail_by_request["provider-request-image"].type,
                detail_by_request["provider-request-image"].input_tokens,
                detail_by_request["provider-request-image"].cached_tokens,
                detail_by_request["provider-request-image"].output_tokens,
            ) == (
                original_key,
                original_id,
                original_name,
                "image-model",
                ProviderUsageType.IMAGE_GENERATION.value,
                8,
                2,
                1,
            )

            daily = list((await db.execute(select(ProviderUsageDaily))).scalars().all())
            assert len(daily) == 2
            daily_by_dimension = {(row.usage_date, row.type, row.model_id): row for row in daily}
            chat_daily = daily_by_dimension[(datetime(2026, 1, 1).date(), ProviderUsageType.MAIN_DIALOGUE.value, "chat-model")]
            assert (
                chat_daily.channel_key,
                chat_daily.channel_id,
                chat_daily.channel_name,
                chat_daily.response_model_id,
                chat_daily.protocol,
                chat_daily.input_tokens,
                chat_daily.cached_tokens,
                chat_daily.output_tokens,
                chat_daily.image_count,
                chat_daily.rerank_search_units,
                chat_daily.request_count,
                chat_daily.input_tokens_reported_count,
                chat_daily.cached_tokens_reported_count,
                chat_daily.output_tokens_reported_count,
                chat_daily.image_count_reported_count,
                chat_daily.rerank_search_units_reported_count,
            ) == (original_key, original_id, original_name, "chat-response-model", "openai", 120, 30, 9, 0, 0, 1, 1, 1, 1, 1, 0)
            image_daily = daily_by_dimension[(datetime(2026, 1, 2).date(), ProviderUsageType.IMAGE_GENERATION.value, "image-model")]
            assert (
                image_daily.channel_key,
                image_daily.channel_id,
                image_daily.channel_name,
                image_daily.response_model_id,
                image_daily.protocol,
                image_daily.input_tokens,
                image_daily.cached_tokens,
                image_daily.output_tokens,
                image_daily.image_count,
                image_daily.request_count,
            ) == (original_key, original_id, original_name, "image-response-model", "openai_image", 8, 2, 1, 2, 1)

            stored_channel = await db.get(ModelChannel, original_id)
            assert stored_channel is not None
            await db.delete(stored_channel)
            await db.commit()

            replacement_channel = _channel(original_name)
            db.add(replacement_channel)
            await db.commit()
            assert replacement_channel.id == original_id
            assert replacement_channel.usage_key != original_key
            replacement_key = replacement_channel.usage_key
            replacement_detail = _detail(
                provider_request_id="provider-request-replacement",
                created_at=_utc_naive(datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)),
                channel=replacement_channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=50,
                cached_tokens=5,
                output_tokens=6,
            )
            assert await provider_usage_crud.create_once(db, replacement_detail) is True
            await db.commit()

        async with factory() as db:
            channels = list((await db.execute(select(ModelChannel))).scalars().all())
            assert [(channel.id, channel.name, channel.usage_key) for channel in channels] == [(original_id, original_name, replacement_key)]

            details = list((await db.execute(select(ProviderUsageDetail))).scalars().all())
            assert {detail.provider_request_id for detail in details} == {
                "provider-request-chat",
                "provider-request-image",
                "provider-request-replacement",
            }
            old_details = [detail for detail in details if detail.provider_request_id != "provider-request-replacement"]
            assert {detail.channel_key for detail in old_details} == {original_key}
            assert {detail.channel_id for detail in old_details} == {original_id}
            assert {detail.channel_name for detail in old_details} == {original_name}
            replacement = next(detail for detail in details if detail.provider_request_id == "provider-request-replacement")
            assert (replacement.channel_key, replacement.channel_id, replacement.channel_name, replacement.model_id) == (
                replacement_key,
                original_id,
                original_name,
                "chat-model",
            )

            daily = list((await db.execute(select(ProviderUsageDaily))).scalars().all())
            old_daily = [row for row in daily if row.channel_key == original_key]
            replacement_daily = [row for row in daily if row.channel_key == replacement_key]
            assert len(old_daily) == 2
            assert len(replacement_daily) == 1
            assert (
                replacement_daily[0].usage_date,
                replacement_daily[0].type,
                replacement_daily[0].model_id,
                replacement_daily[0].input_tokens,
                replacement_daily[0].cached_tokens,
                replacement_daily[0].output_tokens,
                replacement_daily[0].request_count,
            ) == (datetime(2026, 1, 1).date(), ProviderUsageType.MAIN_DIALOGUE.value, "chat-model", 50, 5, 6, 1)


_CLIENT_PROVIDER_USAGE_CASES = (
    pytest.param(
        "embedding",
        {
            "model": "embedding-response-model",
            "object": "list",
            "usage": {"prompt_tokens": 0},
            "data": [{"object": "embedding", "index": 0, "embedding": [0.125, 0.25]}],
        },
        {
            "model_id": "embedding-request-model",
            "protocol": "openai_embedding",
            "channel_usage": "EMBEDDING",
            "usage_type": ProviderUsageType.EMBEDDING,
            "detail_metrics": (0, None, None, None, None),
            "daily_metrics": (0, 0, 0, 0, 0),
            "reported_counts": (1, 0, 0, 0, 0),
        },
        id="embedding-client-provider-usage",
    ),
    pytest.param(
        "rerank",
        {
            "model": "rerank-response-model",
            "meta": {
                "billed_units": {"search_units": 7},
                "tokens": {"input_tokens": 9, "output_tokens": 4},
            },
            "results": [
                {"index": 1, "relevance_score": 0.91},
                {"index": 0, "relevance_score": 0.32},
            ],
        },
        {
            "model_id": "rerank-request-model",
            "protocol": "cohere_rerank",
            "channel_usage": "RERANK",
            "usage_type": ProviderUsageType.RERANK,
            "detail_metrics": (None, None, None, None, 7),
            "daily_metrics": (0, 0, 0, 0, 7),
            "reported_counts": (0, 0, 0, 0, 1),
        },
        id="rerank-client-provider-usage",
    ),
    pytest.param(
        "image",
        {
            "created": 1,
            "model": "image-response-model",
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens": 3,
            },
            "data": [
                {"url": "https://images.invalid/image-1.png"},
                {"url": "https://images.invalid/image-2.png"},
            ],
        },
        {
            "model_id": "image-request-model",
            "protocol": "openai_image",
            "channel_usage": "IMAGE_GENERATION",
            "usage_type": ProviderUsageType.IMAGE_GENERATION,
            "detail_metrics": (20, 5, 3, 2, None),
            "daily_metrics": (20, 5, 3, 2, 0),
            "reported_counts": (1, 1, 1, 1, 0),
        },
        id="image-client-provider-usage",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "response_payload", "expected"),
    _CLIENT_PROVIDER_USAGE_CASES,
)
async def test_non_chat_clients_record_provider_usage_through_real_transformers(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    response_payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    async with database_factory(
        tables=_PROVIDER_USAGE_TABLES,
        foreign_keys=True,
        name=f"provider-usage-{operation}-client-workflow.sqlite3",
    ) as factory:
        model_id = expected["model_id"]
        protocol = expected["protocol"]
        channel = ModelChannel(
            name=f"provider-usage-{operation}-client-channel",
            api_key="enc:v1:provider-usage-client-test-key",
            base_url="https://provider.invalid/v1",
            model_ids=[
                {
                    "model_id": model_id,
                    "usage": expected["channel_usage"],
                    "protocol": protocol.upper(),
                }
            ],
        )
        async with factory() as db:
            db.add(channel)
            await db.commit()
            channel_id = channel.id
            assert channel_id is not None
            persisted_channel = await db.get(ModelChannel, channel_id)
            assert persisted_channel is not None

        source = ProviderUsageSource.from_channel(persisted_channel, model_id=model_id, protocol=protocol)
        response_text = json.dumps(response_payload, ensure_ascii=False)
        requests: list[tuple[str, dict[str, Any]]] = []

        class FakeResponse:
            status = 200

            async def __aenter__(self) -> "FakeResponse":
                return self

            async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
                return False

            async def text(self) -> str:
                return response_text

        class FakeClientSession:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClientSession":
                return self

            async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
                return False

            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                requests.append((url, kwargs))
                return FakeResponse()

        monkeypatch.setattr(aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(aiohttp, "TCPConnector", lambda **_kwargs: object())
        monkeypatch.setattr(provider_usage_module, "AsyncSessionLocal", factory)

        if operation == "image":

            async def load_llm_request_max_bytes() -> int:
                return 32 * 1024 * 1024

            monkeypatch.setattr(image_generation_module.llm_request_policy, "load_llm_request_max_bytes", load_llm_request_max_bytes)

        if operation == "embedding":
            result = await EmbeddingClient.get_embeddings(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id=model_id,
                protocol=protocol,
                input_texts=["embedding input"],
                usage_source=source,
            )
            assert result == response_payload
            assert result["data"][0]["embedding"] == [0.125, 0.25]
        elif operation == "rerank":
            result = await RerankClient.rerank_texts(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id=model_id,
                protocol=protocol,
                query="rerank query",
                documents=["first document", "second document"],
                top_n=2,
                usage_source=source,
            )
            assert [(item.index, item.relevance_score) for item in result] == [(1, 0.91), (0, 0.32)]
        else:
            result = await ImageGenerationClient.generate_image(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id=model_id,
                protocol=protocol,
                prompt="image prompt",
                n=2,
                response_format="url",
                usage_source=source,
            )
            assert result == response_payload
            assert len(result["data"]) == 2

        assert len(requests) == 1

        async with factory() as db:
            stored_channel = await db.get(ModelChannel, channel_id)
            assert stored_channel is not None
            await db.delete(stored_channel)
            await db.commit()

        async with factory() as db:
            assert await db.get(ModelChannel, channel_id) is None
            details = list((await db.execute(select(ProviderUsageDetail))).scalars().all())
            daily_rows = list((await db.execute(select(ProviderUsageDaily))).scalars().all())

        assert len(details) == 1
        assert len(daily_rows) == 1
        detail = details[0]
        daily = daily_rows[0]
        assert (
            detail.type,
            detail.channel_key,
            detail.channel_id,
            detail.channel_name,
            detail.model_id,
            detail.response_model_id,
            detail.protocol,
        ) == (
            expected["usage_type"].value,
            source.channel_key,
            source.channel_id,
            source.channel_name,
            model_id,
            response_payload["model"],
            protocol,
        )
        assert (
            detail.input_tokens,
            detail.cached_tokens,
            detail.output_tokens,
            detail.image_count,
            detail.rerank_search_units,
        ) == expected["detail_metrics"]
        assert (
            daily.type,
            daily.channel_key,
            daily.channel_id,
            daily.channel_name,
            daily.model_id,
            daily.response_model_id,
            daily.protocol,
        ) == (
            expected["usage_type"].value,
            source.channel_key,
            source.channel_id,
            source.channel_name,
            model_id,
            response_payload["model"],
            protocol,
        )
        assert daily.usage_date == detail.created_at.date()
        assert (
            daily.input_tokens,
            daily.cached_tokens,
            daily.output_tokens,
            daily.image_count,
            daily.rerank_search_units,
        ) == expected["daily_metrics"]
        assert (
            daily.input_tokens_reported_count,
            daily.cached_tokens_reported_count,
            daily.output_tokens_reported_count,
            daily.image_count_reported_count,
            daily.rerank_search_units_reported_count,
        ) == expected["reported_counts"]
        assert daily.request_count == 1


@pytest.mark.asyncio
async def test_embedding_client_embed_texts_records_usage_for_each_batch(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with database_factory(
        tables=_PROVIDER_USAGE_TABLES,
        foreign_keys=True,
        name="provider-usage-embedding-batch-workflow.sqlite3",
    ) as factory:
        model_id = "embedding-model"
        protocol = "openai_embedding"
        channel = ModelChannel(
            name="provider-usage-embedding-batch-channel",
            api_key="enc:v1:provider-usage-embedding-batch-test-key",
            base_url="https://provider.invalid/v1",
            model_ids=[
                {
                    "model_id": model_id,
                    "usage": "EMBEDDING",
                    "protocol": "OPENAI_EMBEDDING",
                    "is_enabled": True,
                }
            ],
        )
        async with factory() as db:
            db.add(channel)
            await db.commit()
            channel_id = channel.id
            assert channel_id is not None
            persisted_channel = await db.get(ModelChannel, channel_id)
            assert persisted_channel is not None

        source = ProviderUsageSource.from_channel(persisted_channel, model_id=model_id, protocol=protocol)
        response_payloads = [
            {
                "model": "embedding-response-model",
                "usage": {"prompt_tokens": 12},
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [1.0, 1.1]},
                    {"object": "embedding", "index": 1, "embedding": [1.2, 1.3]},
                ],
            },
            {
                "model": "embedding-response-model",
                "usage": {"prompt_tokens": 0},
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [2.0, 2.1]},
                    {"object": "embedding", "index": 1, "embedding": [2.2, 2.3]},
                ],
            },
            {
                "model": "embedding-response-model",
                "data": [{"object": "embedding", "index": 0, "embedding": [3.0, 3.1]}],
            },
        ]
        requests: list[tuple[str, dict[str, Any]]] = []

        class FakeResponse:
            status = 200

            def __init__(self, payload: dict[str, Any]) -> None:
                self.payload = payload

            async def __aenter__(self) -> "FakeResponse":
                return self

            async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
                return False

            async def text(self) -> str:
                return json.dumps(self.payload, ensure_ascii=False)

        class FakeClientSession:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClientSession":
                return self

            async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
                return False

            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                requests.append((url, kwargs))
                return FakeResponse(response_payloads[len(requests) - 1])

        monkeypatch.setattr(aiohttp, "ClientSession", FakeClientSession)
        monkeypatch.setattr(aiohttp, "TCPConnector", lambda **_kwargs: object())
        monkeypatch.setattr(provider_usage_module, "AsyncSessionLocal", factory)

        result = await EmbeddingClient.embed_texts(
            api_key="provider-api-key",
            base_url="https://provider.invalid/v1",
            model_id=model_id,
            protocol=protocol,
            input_texts=["text-1", "text-2", "text-3", "text-4", "text-5"],
            batch_size=2,
            usage_source=source,
        )

        assert result == [
            [1.0, 1.1],
            [1.2, 1.3],
            [2.0, 2.1],
            [2.2, 2.3],
            [3.0, 3.1],
        ]
        assert len(requests) == 3
        assert [request[1]["json"]["input"] for request in requests] == [
            ["text-1", "text-2"],
            ["text-3", "text-4"],
            ["text-5"],
        ]

        async with factory() as db:
            details = list((await db.execute(select(ProviderUsageDetail))).scalars().all())
            daily_rows = list((await db.execute(select(ProviderUsageDaily))).scalars().all())

        assert len(details) == 3
        assert len(daily_rows) == 1
        assert {detail.input_tokens for detail in details} == {12, 0, None}
        assert all(
            (
                detail.type,
                detail.channel_key,
                detail.channel_id,
                detail.channel_name,
                detail.model_id,
                detail.response_model_id,
                detail.protocol,
                detail.cached_tokens,
                detail.output_tokens,
                detail.image_count,
                detail.rerank_search_units,
            )
            == (
                ProviderUsageType.EMBEDDING.value,
                source.channel_key,
                source.channel_id,
                source.channel_name,
                model_id,
                "embedding-response-model",
                protocol,
                None,
                None,
                None,
                None,
            )
            for detail in details
        )

        daily = daily_rows[0]
        assert (
            daily.type,
            daily.channel_key,
            daily.channel_id,
            daily.channel_name,
            daily.model_id,
            daily.response_model_id,
            daily.protocol,
            daily.input_tokens,
            daily.cached_tokens,
            daily.output_tokens,
            daily.image_count,
            daily.rerank_search_units,
            daily.request_count,
            daily.input_tokens_reported_count,
            daily.cached_tokens_reported_count,
            daily.output_tokens_reported_count,
            daily.image_count_reported_count,
            daily.rerank_search_units_reported_count,
        ) == (
            ProviderUsageType.EMBEDDING.value,
            source.channel_key,
            source.channel_id,
            source.channel_name,
            model_id,
            "embedding-response-model",
            protocol,
            12,
            0,
            0,
            0,
            0,
            3,
            2,
            0,
            0,
            0,
            0,
        )
        assert {detail.created_at.date() for detail in details} == {daily.usage_date}


@pytest.mark.asyncio
async def test_provider_usage_cleanup_uses_24_hour_due_interval_and_strict_detail_cutoff(database_factory) -> None:
    async with database_factory(
        tables=_PROVIDER_USAGE_TABLES,
        foreign_keys=True,
        name="provider-usage-retention-workflow.sqlite3",
    ) as factory:
        cleanup_time = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
        cleanup_time_naive = _utc_naive(cleanup_time)
        channel = _channel("provider-usage-retention-channel")

        async with factory() as db:
            assert (await db.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            db.add(channel)
            await db.commit()

            old_detail = _detail(
                provider_request_id="retention-old",
                created_at=_utc_naive(cleanup_time - timedelta(hours=25)),
                channel=channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=101,
                cached_tokens=11,
                output_tokens=7,
            )
            exact_first_cutoff_detail = _detail(
                provider_request_id="retention-first-cutoff",
                created_at=_utc_naive(cleanup_time - timedelta(hours=24)),
                channel=channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=202,
                cached_tokens=22,
                output_tokens=14,
            )
            assert await provider_usage_crud.create_once(db, old_detail) is True
            assert await provider_usage_crud.create_once(db, exact_first_cutoff_detail) is True
            await db.commit()

        async with factory() as db:
            assert await provider_usage_crud.cleanup_if_due(db, now=cleanup_time) == 1
            await db.commit()
            remaining = list((await db.execute(select(ProviderUsageDetail))).scalars().all())
            assert [detail.provider_request_id for detail in remaining] == ["retention-first-cutoff"]
            marker = (await db.execute(select(SystemSetting))).scalars().one()
            assert marker.value == cleanup_time_naive.isoformat(timespec="microseconds")

        async with factory() as db:
            exact_second_cutoff_detail = _detail(
                provider_request_id="retention-second-cutoff",
                created_at=cleanup_time_naive,
                channel=channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=303,
                cached_tokens=33,
                output_tokens=21,
            )
            twenty_three_hour_detail = _detail(
                provider_request_id="retention-23-hours",
                created_at=_utc_naive(cleanup_time + timedelta(hours=23)),
                channel=channel,
                usage_type=ProviderUsageType.MAIN_DIALOGUE,
                model_id="chat-model",
                protocol="openai",
                response_model_id="chat-response-model",
                input_tokens=404,
                cached_tokens=44,
                output_tokens=28,
            )
            assert await provider_usage_crud.create_once(db, exact_second_cutoff_detail) is True
            assert await provider_usage_crud.create_once(db, twenty_three_hour_detail) is True
            assert await provider_usage_crud.cleanup_if_due(db, now=cleanup_time + timedelta(hours=23)) == 0
            await db.commit()

            remaining = list((await db.execute(select(ProviderUsageDetail).order_by(ProviderUsageDetail.provider_request_id))).scalars().all())
            assert [detail.provider_request_id for detail in remaining] == [
                "retention-23-hours",
                "retention-first-cutoff",
                "retention-second-cutoff",
            ]
            assert (
                remaining[0].input_tokens,
                remaining[0].cached_tokens,
                remaining[0].output_tokens,
            ) == (404, 44, 28)

        async with factory() as db:
            daily_before_rows = list((await db.execute(select(ProviderUsageDaily))).scalars().all())
            daily_before = {
                (row.usage_date, row.type): (
                    row.input_tokens,
                    row.cached_tokens,
                    row.output_tokens,
                    row.image_count,
                    row.rerank_search_units,
                    row.request_count,
                    row.input_tokens_reported_count,
                    row.cached_tokens_reported_count,
                    row.output_tokens_reported_count,
                    row.image_count_reported_count,
                    row.rerank_search_units_reported_count,
                )
                for row in daily_before_rows
            }
            assert daily_before == {
                (cleanup_time_naive.date() - timedelta(days=1), ProviderUsageType.MAIN_DIALOGUE.value): (303, 33, 21, 0, 0, 2, 2, 2, 2, 2, 0),
                (cleanup_time_naive.date(), ProviderUsageType.MAIN_DIALOGUE.value): (303, 33, 21, 0, 0, 1, 1, 1, 1, 1, 0),
                (cleanup_time_naive.date() + timedelta(days=1), ProviderUsageType.MAIN_DIALOGUE.value): (404, 44, 28, 0, 0, 1, 1, 1, 1, 1, 0),
            }
            assert sum(values[0] for values in daily_before.values()) == 1010
            assert sum(values[1] for values in daily_before.values()) == 110
            assert sum(values[2] for values in daily_before.values()) == 70

            assert await provider_usage_crud.cleanup_if_due(db, now=cleanup_time + timedelta(hours=24)) == 1
            await db.commit()

            daily_after_rows = list((await db.execute(select(ProviderUsageDaily))).scalars().all())
            daily_after = {
                (row.usage_date, row.type): (
                    row.input_tokens,
                    row.cached_tokens,
                    row.output_tokens,
                    row.image_count,
                    row.rerank_search_units,
                    row.request_count,
                    row.input_tokens_reported_count,
                    row.cached_tokens_reported_count,
                    row.output_tokens_reported_count,
                    row.image_count_reported_count,
                    row.rerank_search_units_reported_count,
                )
                for row in daily_after_rows
            }
            assert daily_after == daily_before

            remaining = list((await db.execute(select(ProviderUsageDetail).order_by(ProviderUsageDetail.provider_request_id))).scalars().all())
            assert [detail.provider_request_id for detail in remaining] == ["retention-23-hours", "retention-second-cutoff"]
            remaining_by_request = {detail.provider_request_id: detail for detail in remaining}
            assert (
                remaining_by_request["retention-second-cutoff"].created_at,
                remaining_by_request["retention-second-cutoff"].input_tokens,
                remaining_by_request["retention-second-cutoff"].cached_tokens,
                remaining_by_request["retention-second-cutoff"].output_tokens,
            ) == (cleanup_time_naive, 303, 33, 21)
            assert (
                remaining_by_request["retention-23-hours"].input_tokens,
                remaining_by_request["retention-23-hours"].cached_tokens,
                remaining_by_request["retention-23-hours"].output_tokens,
            ) == (404, 44, 28)


_UPSTREAM_USAGE_LOG_CASES = (
    pytest.param(
        "embedding",
        {
            "model": "embedding-response-model",
            "object": "list",
            "usage": {
                "prompt_tokens": 12,
                "total_tokens": 15,
                "optional_metric": None,
                "negative_metric": -3,
                "sensitive_usage": "embedding-usage-secret",
            },
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.125, 0.25],
                    "text": "embedding-result-secret",
                }
            ],
        },
        {
            "usage": {
                "prompt_tokens": 12,
                "total_tokens": 15,
                "optional_metric": None,
                "negative_metric": "int",
                "sensitive_usage": "str",
            }
        },
        ("embedding-input-secret", "embedding-usage-secret", "embedding-result-secret", "[0.125, 0.25]"),
        1,
        id="openai-embedding",
    ),
    pytest.param(
        "rerank",
        {
            "model": "rerank-response-model",
            "meta": {
                "billed_units": {
                    "search_units": 7,
                    "optional_units": None,
                    "negative_units": -2,
                    "sensitive_units": "rerank-unit-secret",
                },
                "tokens": {
                    "input_tokens": 9,
                    "output_tokens": 4,
                    "optional_tokens": None,
                    "sensitive_tokens": "rerank-token-secret",
                },
            },
            "results": [
                {
                    "index": 1,
                    "relevance_score": 0.91,
                    "document": "rerank-result-document-secret",
                },
                {"index": 0, "relevance_score": 0.32},
            ],
        },
        {
            "usage": None,
            "billed_units": {
                "search_units": 7,
                "optional_units": None,
                "negative_units": "int",
                "sensitive_units": "str",
            },
            "tokens": {
                "input_tokens": 9,
                "output_tokens": 4,
                "optional_tokens": None,
                "sensitive_tokens": "str",
            },
        },
        ("rerank-query-secret", "rerank-document-secret", "rerank-unit-secret", "rerank-token-secret", "rerank-result-document-secret"),
        2,
        id="cohere-rerank",
    ),
    pytest.param(
        "image",
        {
            "created": 1,
            "model": "image-response-model",
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 5, "optional_cached": None, "sensitive_cached": "image-cache-secret"},
                "output_tokens": 3,
                "optional_tokens": None,
                "negative_tokens": -1,
                "sensitive_usage": "image-usage-secret",
            },
            "data": [{"url": "https://images.invalid/image-secret.png", "b64_json": "image-result-secret"}],
        },
        {
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 5, "optional_cached": None, "sensitive_cached": "str"},
                "output_tokens": 3,
                "optional_tokens": None,
                "negative_tokens": "int",
                "sensitive_usage": "str",
            }
        },
        ("image-prompt-secret", "image-cache-secret", "image-usage-secret", "https://images.invalid/image-secret.png", "image-result-secret"),
        1,
        id="openai-image-generation",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "response_payload", "expected_provider_usage", "sensitive_values", "expected_result_count"),
    _UPSTREAM_USAGE_LOG_CASES,
)
async def test_non_chat_transformers_log_redacted_upstream_usage_without_mutating_response(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    response_payload: dict[str, Any],
    expected_provider_usage: dict[str, Any],
    sensitive_values: tuple[str, ...],
    expected_result_count: int,
) -> None:
    response_before = copy.deepcopy(response_payload)
    response_text = json.dumps(response_payload, ensure_ascii=False)

    class FakeResponse:
        status = 200

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
            return False

        async def text(self) -> str:
            return response_text

    class FakeClientSession:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClientSession":
            return self

        async def __aexit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> bool:
            return False

        def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **_kwargs: object())

    if operation == "image":

        async def load_llm_request_max_bytes() -> int:
            return 32 * 1024 * 1024

        monkeypatch.setattr(image_generation_module.llm_request_policy, "load_llm_request_max_bytes", load_llm_request_max_bytes)

    captured: list[dict[str, Any]] = []

    def capture(message: Any) -> None:
        record = message.record
        if "provider_usage" in record["extra"]:
            captured.append(
                {
                    "extra": copy.deepcopy(record["extra"]),
                    "level": record["level"].name,
                    "message": record["message"],
                }
            )

    sink_id = logger.add(capture, level="DEBUG", enqueue=False)
    try:
        if operation == "embedding":
            result = await OpenAIEmbeddingTransformer().get_embeddings(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id="embedding-request-model",
                input_texts=["embedding-input-secret"],
            )
        elif operation == "rerank":
            result = await CohereRerankTransformer().get_rerank(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id="rerank-request-model",
                query="rerank-query-secret",
                documents=["rerank-document-secret"],
                top_n=1,
            )
        else:
            result = await OpenAIImageGenerationTransformer().generate_image(
                api_key="provider-api-key",
                base_url="https://provider.invalid/v1",
                model_id="image-request-model",
                prompt="image-prompt-secret",
                response_format="b64_json",
            )
    finally:
        logger.remove(sink_id)

    assert result == response_before
    assert response_payload == response_before
    assert len(captured) == 1

    record = captured[0]
    assert record["level"] == "DEBUG"
    provider_usage = record["extra"]["provider_usage"]
    assert provider_usage == expected_provider_usage
    assert str(expected_provider_usage) in record["message"]
    assert f"result_count={expected_result_count}" in record["message"]
    assert all(secret not in record["message"] for secret in sensitive_values)
    assert all(secret not in json.dumps(provider_usage, ensure_ascii=False) for secret in sensitive_values)
