import pytest

from app.api.v1.profile import validate_channel_configs
from app.core.exceptions import ParameterException


@pytest.mark.asyncio
async def test_validate_channel_configs_skips_when_rerank_not_configured():
    await validate_channel_configs(channel_config={})


@pytest.mark.asyncio
async def test_validate_channel_configs_rejects_invalid_rerank_channel():
    config = {
        "rerank_channel": {
            "rerank_candidate_k": 20,
            "kb_query_top_k": 5,
            "rules": [{"channel_id": 1, "model_id": "", "priority": 1, "weight": 100}],
        }
    }
    with pytest.raises(ParameterException):
        await validate_channel_configs(channel_config=config)


@pytest.mark.asyncio
async def test_validate_channel_configs_rejects_candidate_k_less_than_top_k():
    config = {
        "rerank_channel": {
            "rerank_candidate_k": 3,
            "kb_query_top_k": 5,
            "rules": [{"channel_id": 1, "model_id": "rerank-model-id", "priority": 1, "weight": 100}],
        }
    }
    with pytest.raises(ParameterException):
        await validate_channel_configs(channel_config=config)


@pytest.mark.asyncio
async def test_validate_channel_configs_accepts_candidate_k_ge_top_k():
    config = {
        "rerank_channel": {
            "rerank_candidate_k": 20,
            "kb_query_top_k": 5,
            "rules": [{"channel_id": 1, "model_id": "rerank-model-id", "priority": 1, "weight": 100}],
        }
    }
    await validate_channel_configs(channel_config=config)


@pytest.mark.asyncio
async def test_validate_channel_configs_accepts_chat_and_embedding_channels():
    config = {
        "chat_channel": {
            "rules": [{"channel_id": 1, "model_id": "gpt-4o", "priority": 1, "weight": 100}],
        },
        "embedding_channel": {
            "rules": [{"channel_id": 1, "model_id": "text-embedding-3-small", "priority": 1, "weight": 100}],
        },
    }
    await validate_channel_configs(channel_config=config)
