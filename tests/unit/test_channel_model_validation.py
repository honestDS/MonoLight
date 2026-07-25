import pytest
from pydantic import ValidationError

from app.models.channel import ChannelModelItem, ModelProtocol, ModelUsage, resolve_model_protocol


@pytest.mark.parametrize("usage", list(ModelUsage))
def test_model_requires_protocol(usage: ModelUsage) -> None:
    with pytest.raises(ValidationError):
        ChannelModelItem.model_validate({"model_id": "model", "usage": usage})


@pytest.mark.parametrize(
    ("usage", "protocol"),
    [
        (ModelUsage.CHAT, ModelProtocol.OPENAI),
        (ModelUsage.CHAT, ModelProtocol.OPENAI_RESPONSES),
        (ModelUsage.EMBEDDING, ModelProtocol.OPENAI_EMBEDDING),
        (ModelUsage.RERANK, ModelProtocol.COHERE_RERANK),
        (ModelUsage.IMAGE_GENERATION, ModelProtocol.OPENAI_IMAGE),
    ],
)
def test_model_accepts_matching_protocol(usage: ModelUsage, protocol: ModelProtocol) -> None:
    model_entry = ChannelModelItem.model_validate(
        {
            "model_id": "model",
            "usage": usage,
            "protocol": protocol,
        }
    )

    assert model_entry.protocol == protocol


@pytest.mark.parametrize(
    ("usage", "protocol"),
    [
        (ModelUsage.CHAT, ModelProtocol.OPENAI_EMBEDDING),
        (ModelUsage.EMBEDDING, ModelProtocol.OPENAI),
        (ModelUsage.RERANK, ModelProtocol.OPENAI_IMAGE),
        (ModelUsage.IMAGE_GENERATION, ModelProtocol.COHERE_RERANK),
    ],
)
def test_model_rejects_protocol_for_different_usage(usage: ModelUsage, protocol: ModelProtocol) -> None:
    with pytest.raises(ValidationError):
        ChannelModelItem.model_validate(
            {
                "model_id": "model",
                "usage": usage,
                "protocol": protocol,
            }
        )


@pytest.mark.parametrize(
    ("protocol", "expected_protocol"),
    [
        (ModelProtocol.OPENAI, "openai"),
        (ModelProtocol.OPENAI_RESPONSES, "openai_responses"),
    ],
)
def test_resolve_model_protocol_returns_lowercase_client_key(
    protocol: ModelProtocol,
    expected_protocol: str,
) -> None:
    assert resolve_model_protocol({"protocol": protocol}) == expected_protocol
