from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient

CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS = 600.0


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
