from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient, estimate_request_context_tokens

CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS = 600.0


def estimate_context_summary_prompt_tokens(
    model: ContextSummaryModelSnapshot,
    prompt: str,
) -> int:
    return estimate_request_context_tokens(
        [InternalMessage(role=MessageRole.USER, content=prompt)],
        None,
        model_id=model.model_id,
        protocol=model.protocol,
    )


def estimate_context_summary_text_tokens(
    model: ContextSummaryModelSnapshot,
    text: str,
) -> int:
    return estimate_tokens(
        text,
        model_id=model.model_id,
        protocol=model.protocol,
    )


async def call_context_summary_model(
    *,
    model: ContextSummaryModelSnapshot,
    prompt: str,
) -> str | None:
    generation_params = {
        "max_tokens": model.max_output_tokens,
        **({"temperature": model.temperature} if model.temperature is not None else {}),
        **({"top_p": model.top_p} if model.top_p is not None else {}),
        **({"reasoning_effort": model.reasoning_effort} if model.reasoning_effort is not None else {}),
    }
    response = await LLMClient.generate(
        api_key=model.api_key,
        base_url=model.base_url,
        model_id=model.model_id,
        messages=[InternalMessage(role=MessageRole.USER, content=prompt)],
        **generation_params,
        protocol=model.protocol,
        timeout=CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
        http_proxy=model.http_proxy,
        custom_headers=model.custom_headers,
    )
    summary = (response.message.content or "").strip()
    return summary or None
