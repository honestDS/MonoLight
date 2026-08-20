from types import SimpleNamespace

import pytest

from app.core.embedding import knowledge_base as embedding_knowledge_base
from app.core.rerank import knowledge_base as rerank_knowledge_base


def test_get_profile_kb_query_top_k_reads_explicit_knowledge_config() -> None:
    profile = SimpleNamespace(
        configs={
            "memory": {
                "knowledge": {
                    "top_k": 13,
                }
            }
        }
    )

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == 13


def test_get_profile_kb_query_top_k_supports_legacy_rerank_config() -> None:
    legacy_top_k = 9
    profile = SimpleNamespace(
        configs={
            "channel": {
                "rerank_channel": {
                    "kb_query_top_k": legacy_top_k,
                }
            }
        }
    )

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == legacy_top_k


@pytest.mark.parametrize(
    "configs",
    [
        {"memory": {"knowledge": {"top_k": 0}}},
        {"memory": {"knowledge": {"top_k": "not-a-number"}}},
    ],
)
def test_get_profile_kb_query_top_k_falls_back_for_invalid_profile_config(configs: dict) -> None:
    profile = SimpleNamespace(configs=configs)

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == embedding_knowledge_base.KNOWLEDGE_BASE_QUERY_TOP_K


@pytest.mark.asyncio
async def test_get_profile_rerank_config_uses_knowledge_candidate_k(monkeypatch) -> None:
    channel = SimpleNamespace(
        id=17,
        name="rerank-channel",
        base_url="https://rerank.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "api-key",
    )
    model_entry = {
        "model_id": "rerank-model",
        "usage": "RERANK",
        "protocol": "COHERE_RERANK",
    }
    rerank_channel = {
        "rerank_candidate_k": 49,
        "rules": [
            {
                "channel_id": 17,
                "model_id": "rerank-model",
                "priority": 2,
                "weight": 100,
            }
        ],
    }
    profile = SimpleNamespace(
        id=23,
        configs={
            "channel": {"rerank_channel": rerank_channel},
            "memory": {
                "knowledge": {
                    "top_k": 8,
                    "candidate_k": 31,
                    "result_max_chars": 4000,
                }
            },
        },
    )

    async def fake_select_channel(_db, channel_config, expected_usage, **_kwargs):
        assert expected_usage == "RERANK"
        return channel, model_entry, channel_config.rules[0]

    monkeypatch.setattr(rerank_knowledge_base, "select_channel", fake_select_channel)

    result = await rerank_knowledge_base.get_profile_rerank_config(object(), profile)

    assert result is not None
    assert result.candidate_k == 31
