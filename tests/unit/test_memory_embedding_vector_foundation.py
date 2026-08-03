import importlib
import re
import sys
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import chromadb
import pytest
from fastapi import HTTPException

from app.core import paths as app_paths
from app.core.constants import (
    ERR_DENSE_RETRIEVAL_FAILED,
    ERR_EMBEDDING_VECTOR_EMPTY,
    ERR_KB_DENSE_RETRIEVAL_FAILED,
    ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED,
    ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL,
    ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND,
    ERR_PROFILE_NO_EMBEDDING_MODEL,
    ERR_VECTOR_ITEM_LENGTH_MISMATCH,
)
from app.core.i18n import t
from app.models.knowledge_base import KnowledgeBase


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs):
        pass


_VECTOR_MODULE_PREFIX = "app.providers.vector"
_MISSING = object()
_PREEXISTING_VECTOR_MODULES = {name: module for name, module in sys.modules.items() if name == _VECTOR_MODULE_PREFIX or name.startswith(f"{_VECTOR_MODULE_PREFIX}.")}
_PROVIDERS_MODULE = sys.modules.get("app.providers")
_PREVIOUS_VECTOR_ATTRIBUTE = getattr(_PROVIDERS_MODULE, "vector", _MISSING)
_PREVIOUS_CHROMA_CLIENT = getattr(
    sys.modules.get(f"{_VECTOR_MODULE_PREFIX}.chroma"),
    "chroma_client",
    _MISSING,
)


with (
    patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient),
    patch.object(app_paths, "ensure_data_dirs"),
):
    embedding_common = importlib.import_module("app.core.embedding.common")
    knowledge_base_embedding = importlib.import_module("app.core.embedding.knowledge_base")
    hybrid_module = importlib.import_module("app.core.retrieval.hybrid")
    chroma_module = importlib.import_module("app.providers.vector.chroma")

if _PREVIOUS_CHROMA_CLIENT is not _MISSING:
    chroma_module.chroma_client = _PREVIOUS_CHROMA_CLIENT


def teardown_module(_module) -> None:
    """Restore vector modules without constructing a real Chroma client."""
    for name in list(sys.modules):
        if name == _VECTOR_MODULE_PREFIX or name.startswith(f"{_VECTOR_MODULE_PREFIX}."):
            if name not in _PREEXISTING_VECTOR_MODULES:
                del sys.modules[name]

    for name, module in _PREEXISTING_VECTOR_MODULES.items():
        sys.modules[name] = module

    if _PROVIDERS_MODULE is not None:
        if _PREVIOUS_VECTOR_ATTRIBUTE is _MISSING:
            delattr(_PROVIDERS_MODULE, "vector")
        else:
            _PROVIDERS_MODULE.vector = _PREVIOUS_VECTOR_ATTRIBUTE

    if _PREVIOUS_CHROMA_CLIENT is not _MISSING:
        previous_chroma_module = sys.modules.get(f"{_VECTOR_MODULE_PREFIX}.chroma")
        if previous_chroma_module is not None:
            previous_chroma_module.chroma_client = _PREVIOUS_CHROMA_CLIENT


class _FakeChannel:
    def __init__(self, *, channel_id: int = 7, is_active: bool = True, base_url: str | None = "https://embedding.example/v1", model_ids: list[dict] | None = None):
        self.id = channel_id
        self.name = "embedding-channel"
        self.is_active = is_active
        self.base_url = base_url
        self.model_ids = model_ids or []
        self.http_proxy = "http://proxy-user:proxy-pass@proxy.example.com:8080"

    def get_decrypted_api_key(self) -> str:
        return "api-key-secret"


def _embedding_model(**overrides: object) -> dict:
    model = {
        "model_id": "embedding-model",
        "usage": "EMBEDDING",
        "protocol": "OPENAI_EMBEDDING",
        "embedding_dimensions": 1536,
        "embedding_timeout": 45.5,
        "is_enabled": True,
        "advanced_settings": {
            "custom_headers": {
                "X-Trace-Id": "header-value-secret",
                "User-Agent": "MemoryTest/1.0",
            }
        },
    }
    model.update(overrides)
    return model


def _runtime_config() -> embedding_common.EmbeddingRuntimeConfig:
    return embedding_common.EmbeddingRuntimeConfig(
        channel_id=7,
        channel_name="embedding-channel",
        model_id="embedding-model",
        declared_dimensions=1536,
        protocol="openai_embedding",
        timeout=45.5,
        base_url="https://embedding.example/v1",
        api_key="api-key-secret",
        http_proxy="http://proxy-user:proxy-pass@proxy.example.com:8080",
        custom_headers={"x-trace-id": "header-value-secret"},
    )


@pytest.mark.asyncio
async def test_load_embedding_runtime_config_filters_channel_and_model_and_detaches_secrets(monkeypatch) -> None:
    channel = _FakeChannel(
        model_ids=[
            _embedding_model(model_id="wrong-model"),
            _embedding_model(usage="CHAT"),
            _embedding_model(is_enabled=False),
            _embedding_model(),
        ]
    )

    async def fake_get(_db, channel_id: int):
        assert channel_id == 7
        return channel

    monkeypatch.setattr(embedding_common.channel_crud, "get", fake_get)

    config = await embedding_common.load_embedding_runtime_config(object(), 7, "embedding-model")

    assert config.channel_id == 7
    assert config.model_id == "embedding-model"
    assert config.protocol == "openai_embedding"
    assert config.timeout == 45.5
    assert config.declared_dimensions == 1536
    assert config.http_proxy == "http://proxy-user:proxy-pass@proxy.example.com:8080"
    assert dict(config.custom_headers) == {
        "x-trace-id": "header-value-secret",
        "user-agent": "MemoryTest/1.0",
    }

    with pytest.raises(FrozenInstanceError):
        config.timeout = 10.0
    with pytest.raises(TypeError):
        config.custom_headers["x-new"] = "value"

    rendered = repr(config)
    assert "api-key-secret" not in rendered
    assert "proxy-user" not in rendered
    assert "proxy-pass" not in rendered
    assert "header-value-secret" not in rendered
    assert "MemoryTest/1.0" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "model_id", "channel_status", "model_status", "detail"),
    [
        (None, "embedding-model", 418, 419, ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND),
        (
            _FakeChannel(is_active=False, model_ids=[_embedding_model()]),
            "embedding-model",
            418,
            419,
            ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED,
        ),
        (
            _FakeChannel(base_url=None, model_ids=[_embedding_model()]),
            "embedding-model",
            418,
            419,
            ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL,
        ),
        (
            _FakeChannel(model_ids=[_embedding_model(model_id="other-model"), _embedding_model(is_enabled=False)]),
            "embedding-model",
            418,
            419,
            ERR_PROFILE_NO_EMBEDDING_MODEL,
        ),
    ],
)
async def test_load_embedding_runtime_config_preserves_status_override_and_error_semantics(
    monkeypatch,
    channel: _FakeChannel | None,
    model_id: str,
    channel_status: int,
    model_status: int,
    detail: str,
) -> None:
    async def fake_get(_db, _channel_id: int):
        return channel

    monkeypatch.setattr(embedding_common.channel_crud, "get", fake_get)

    with pytest.raises(HTTPException) as exc_info:
        await embedding_common.load_embedding_runtime_config(
            object(),
            7,
            model_id,
            channel_not_found_status_code=channel_status,
            model_not_found_status_code=model_status,
        )

    expected_status = channel_status if channel is None else 400 if not channel.is_active or not channel.base_url else model_status
    assert exc_info.value.status_code == expected_status
    assert exc_info.value.detail == detail


@pytest.mark.asyncio
async def test_embed_texts_with_config_commits_before_forwarding_all_arguments(monkeypatch) -> None:
    config = _runtime_config()
    events: list[object] = []

    class FakeDB:
        async def commit(self) -> None:
            events.append("commit")

    async def fake_embed_texts(**kwargs):
        events.append(("embed", kwargs))
        return [[0.1, 0.2]]

    monkeypatch.setattr(embedding_common.EmbeddingClient, "embed_texts", fake_embed_texts)

    result = await embedding_common.embed_texts_with_config(
        config,
        ["first", "second"],
        batch_size=7,
        dimensions=2,
        db=FakeDB(),
        release_connection=True,
    )

    assert result == [[0.1, 0.2]]
    assert events == [
        "commit",
        (
            "embed",
            {
                "api_key": "api-key-secret",
                "base_url": "https://embedding.example/v1",
                "model_id": "embedding-model",
                "protocol": "openai_embedding",
                "input_texts": ["first", "second"],
                "batch_size": 7,
                "dimensions": 2,
                "timeout": 45.5,
                "http_proxy": "http://proxy-user:proxy-pass@proxy.example.com:8080",
                "custom_headers": {"x-trace-id": "header-value-secret"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_detect_embedding_dimensions_uses_actual_vector_and_omits_declared_dimensions(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_embed_texts(**kwargs):
        calls.append(kwargs)
        return [[1.0, 2.0, 3.0]]

    monkeypatch.setattr(embedding_common.EmbeddingClient, "embed_texts", fake_embed_texts)

    dimensions = await embedding_common.detect_embedding_dimensions(_runtime_config())

    assert dimensions == 3
    assert calls == [
        {
            "api_key": "api-key-secret",
            "base_url": "https://embedding.example/v1",
            "model_id": "embedding-model",
            "protocol": "openai_embedding",
            "input_texts": ["dimension test"],
            "batch_size": 1,
            "dimensions": None,
            "timeout": 45.5,
            "http_proxy": "http://proxy-user:proxy-pass@proxy.example.com:8080",
            "custom_headers": {"x-trace-id": "header-value-secret"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [[], [[]]])
async def test_detect_embedding_dimensions_rejects_empty_vectors(monkeypatch, response) -> None:
    async def fake_embed_texts(**_kwargs):
        return response

    monkeypatch.setattr(embedding_common.EmbeddingClient, "embed_texts", fake_embed_texts)

    with pytest.raises(ValueError, match=re.escape(t(ERR_EMBEDDING_VECTOR_EMPTY))):
        await embedding_common.detect_embedding_dimensions(_runtime_config())


@pytest.mark.asyncio
async def test_embed_chunks_with_knowledge_base_config_preserves_kb_embedding_arguments(monkeypatch) -> None:
    db = object()
    kb = KnowledgeBase(
        uid="user-1",
        name="test-kb",
        embedding_channel_id=11,
        embedding_model_id="embedding-model",
        embedding_dimensions=768,
        collection_name="test-collection",
    )
    config = _runtime_config()
    loader_calls: list[tuple[object, int, str]] = []
    embed_calls: list[tuple[object, list[str], dict]] = []

    async def fake_loader(actual_db, channel_id: int, model_id: str):
        loader_calls.append((actual_db, channel_id, model_id))
        return config

    async def fake_embed(actual_config, texts, **kwargs):
        embed_calls.append((actual_config, texts, kwargs))
        return [[0.5, 0.6]]

    monkeypatch.setattr(knowledge_base_embedding, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(knowledge_base_embedding, "embed_texts_with_config", fake_embed)

    result = await knowledge_base_embedding.embed_chunks_with_knowledge_base_config(
        db,
        kb,
        ["chunk one", "chunk two"],
        batch_size=23,
        release_connection=True,
    )

    assert result == [[0.5, 0.6]]
    assert loader_calls == [(db, 11, "embedding-model")]
    assert embed_calls == [
        (
            config,
            ["chunk one", "chunk two"],
            {
                "batch_size": 23,
                "dimensions": 768,
                "db": db,
                "release_connection": True,
            },
        )
    ]


class _FakeCollection:
    def __init__(self, *, metadata: dict | None = None, count: int = 0, sample: dict | None = None):
        self.metadata = metadata or {}
        self._count = count
        self.sample = sample or {}
        self.upsert_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.delete_calls: list[list[str]] = []

    def upsert(self, **kwargs) -> None:
        self.upsert_calls.append(kwargs)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.sample

    def delete(self, *, ids: list[str]) -> None:
        self.delete_calls.append(ids)

    def count(self) -> int:
        return self._count


class _NoBoolSequence:
    def __init__(self, values):
        self.values = list(values)

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __bool__(self):
        raise AssertionError("the vector container must not be evaluated as a boolean")


class _FakeChromaClient:
    def __init__(self):
        self.create_calls: list[dict] = []

    def create_collection(self, **kwargs):
        self.create_calls.append(kwargs)
        return "created-collection"


@pytest.mark.asyncio
async def test_async_create_collection_adds_cosine_metadata_without_real_chroma(monkeypatch) -> None:
    client = _FakeChromaClient()
    monkeypatch.setattr(chroma_module, "chroma_client", client)

    result = await chroma_module.async_create_collection(
        "memory-collection",
        metadata={"owner": "user-1"},
        distance="cosine",
    )

    assert result == "created-collection"
    assert client.create_calls == [
        {
            "name": "memory-collection",
            "metadata": {"owner": "user-1", "hnsw:space": "cosine"},
        }
    ]


@pytest.mark.asyncio
async def test_async_upsert_collection_items_batches_and_rejects_length_mismatch(monkeypatch) -> None:
    collection = _FakeCollection()
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    count = await chroma_module.async_upsert_collection_items(
        "memory-collection",
        ["1", "2", "3", "4", "5"],
        ["a", "b", "c", "d", "e"],
        [[1.0], [2.0], [3.0], [4.0], [5.0]],
        [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}, {"n": 5}],
        batch_size=2,
    )

    assert count == 5
    assert collection.upsert_calls == [
        {
            "ids": ["1", "2"],
            "documents": ["a", "b"],
            "embeddings": [[1.0], [2.0]],
            "metadatas": [{"n": 1}, {"n": 2}],
        },
        {
            "ids": ["3", "4"],
            "documents": ["c", "d"],
            "embeddings": [[3.0], [4.0]],
            "metadatas": [{"n": 3}, {"n": 4}],
        },
        {
            "ids": ["5"],
            "documents": ["e"],
            "embeddings": [[5.0]],
            "metadatas": [{"n": 5}],
        },
    ]

    with pytest.raises(ValueError, match=re.escape(t(ERR_VECTOR_ITEM_LENGTH_MISMATCH))):
        await chroma_module.async_upsert_collection_items(
            "memory-collection",
            ["1", "2"],
            ["a"],
            [[1.0], [2.0]],
            [{"n": 1}, {"n": 2}],
            batch_size=2,
        )


@pytest.mark.asyncio
async def test_async_get_collection_items_forwards_offset_limit_and_include(monkeypatch) -> None:
    collection = _FakeCollection(sample={"ids": ["3"], "documents": ["document"]})
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    result = await chroma_module.async_get_collection_items(
        "memory-collection",
        offset=2,
        limit=3,
        include=["documents"],
    )

    assert result == {"ids": ["3"], "documents": ["document"]}
    assert collection.get_calls == [{"offset": 2, "include": ["documents"], "limit": 3}]


@pytest.mark.asyncio
async def test_async_delete_collection_items_batches_deletes(monkeypatch) -> None:
    collection = _FakeCollection()
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    count = await chroma_module.async_delete_collection_items(
        "memory-collection",
        ["1", "2", "3", "4", "5"],
        batch_size=2,
    )

    assert count == 5
    assert collection.delete_calls == [["1", "2"], ["3", "4"], ["5"]]


@pytest.mark.asyncio
async def test_async_validate_collection_accepts_custom_vector_container_and_validates_dimensions(monkeypatch) -> None:
    collection = _FakeCollection(
        metadata={"hnsw:space": "cosine", "embedding_model": "memory-model"},
        count=2,
        sample={
            "embeddings": _NoBoolSequence(
                [
                    _NoBoolSequence([0.1, 0.2, 0.3]),
                    _NoBoolSequence([0.4, 0.5, 0.6]),
                ]
            )
        },
    )
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    result = await chroma_module.async_validate_collection(
        "memory-collection",
        expected_count=2,
        expected_metadata={"hnsw:space": "cosine", "embedding_model": "memory-model"},
        expected_dimension=3,
        sample_size=2,
    )

    assert result.exists is True
    assert result.valid is True
    assert result.count == 2
    assert result.sample_dimension == 3
    assert result.errors == ()
    assert collection.get_calls == [{"limit": 2, "include": ["embeddings"]}]


@pytest.mark.asyncio
async def test_async_validate_collection_reports_count_metadata_and_dimension_mismatches(monkeypatch) -> None:
    collection = _FakeCollection(
        metadata={"hnsw:space": "l2"},
        count=2,
        sample={"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
    )
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    result = await chroma_module.async_validate_collection(
        "memory-collection",
        expected_count=3,
        expected_metadata={"hnsw:space": "cosine"},
        expected_dimension=3,
    )

    assert result.exists is True
    assert result.valid is False
    assert result.errors == ("count_mismatch", "metadata_mismatch:hnsw:space", "dimension_mismatch")


@pytest.mark.asyncio
async def test_async_validate_collection_returns_not_found_result_but_propagates_other_errors(monkeypatch) -> None:
    class FakeNotFoundError(Exception):
        pass

    monkeypatch.setattr(chroma_module, "ChromaNotFoundError", FakeNotFoundError)

    def raise_not_found(_name):
        raise FakeNotFoundError("missing")

    monkeypatch.setattr(chroma_module, "get_collection", raise_not_found)

    result = await chroma_module.async_validate_collection("missing-collection")

    assert result.exists is False
    assert result.valid is False
    assert result.errors == ("collection_not_found",)

    def raise_runtime(_name):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(chroma_module, "get_collection", raise_runtime)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await chroma_module.async_validate_collection("broken-collection")


@pytest.mark.asyncio
async def test_async_delete_orphan_items_discovers_and_deletes_in_batches(monkeypatch) -> None:
    collection = _FakeCollection(count=5)
    pages = {
        0: {"ids": ["valid-1", "orphan-1"]},
        2: {"ids": ["orphan-2", "valid-2"]},
        4: {"ids": ["orphan-3"]},
    }

    def get_page(**kwargs):
        collection.get_calls.append(kwargs)
        return pages[kwargs["offset"]]

    collection.get = get_page
    monkeypatch.setattr(chroma_module, "get_collection", lambda _name: collection)

    deleted_count = await chroma_module.async_delete_orphan_items(
        "memory-collection",
        {"valid-1", "valid-2"},
        batch_size=2,
    )

    assert deleted_count == 3
    assert collection.get_calls == [
        {"offset": 0, "limit": 2, "include": ["documents"]},
        {"offset": 2, "limit": 2, "include": ["documents"]},
        {"offset": 4, "limit": 2, "include": ["documents"]},
    ]
    assert collection.delete_calls == [["orphan-1", "orphan-2"], ["orphan-3"]]


@pytest.mark.asyncio
async def test_hybrid_query_collection_uses_generic_dense_error_when_dense_search_fails(monkeypatch) -> None:
    def raise_dense(*_args):
        raise RuntimeError("dense backend failed")

    def empty_sparse(*_args):
        return []

    monkeypatch.setattr(hybrid_module, "dense_search", raise_dense)
    monkeypatch.setattr(hybrid_module, "sparse_search", empty_sparse)

    with pytest.raises(HTTPException) as exc_info:
        await hybrid_module.hybrid_query_collection(
            "memory-collection",
            [0.1, 0.2],
            "query",
            limit=5,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == t(ERR_DENSE_RETRIEVAL_FAILED)


@pytest.mark.asyncio
async def test_hybrid_query_knowledge_base_preserves_legacy_dense_error(monkeypatch) -> None:
    def raise_dense(*_args):
        raise RuntimeError("dense backend failed")

    def empty_sparse(*_args):
        return []

    monkeypatch.setattr(hybrid_module, "dense_search", raise_dense)
    monkeypatch.setattr(hybrid_module, "sparse_search", empty_sparse)

    with pytest.raises(HTTPException) as exc_info:
        await hybrid_module.hybrid_query_knowledge_base(
            "memory-collection",
            [0.1, 0.2],
            "query",
            top_k=5,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == t(ERR_KB_DENSE_RETRIEVAL_FAILED)
