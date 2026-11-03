from collections.abc import Awaitable, Callable

from app.core.constants import CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS, ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED
from app.core.exceptions import LLMException
from app.core.utils.tokenizer import estimate_tokens


async def refine_context_summary_to_fit(
    summary: str,
    *,
    input_budget_tokens: int,
    measure_request_tokens: Callable[[str], int],
    refine_summary: Callable[[str, int], Awaitable[str]],
    model_id: str | None = None,
    protocol: str | None = None,
) -> str:
    refinement_index = 1
    while True:
        if measure_request_tokens(summary) <= input_budget_tokens:
            return summary

        previous_summary_tokens = max(
            1,
            estimate_tokens(
                summary,
                model_id=model_id,
                protocol=protocol,
            ),
        )
        refined_summary = await refine_summary(summary, refinement_index)
        if not isinstance(refined_summary, str) or not refined_summary.strip():
            raise LLMException(message=ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED)

        refined_summary_tokens = max(
            1,
            estimate_tokens(
                refined_summary,
                model_id=model_id,
                protocol=protocol,
            ),
        )
        if previous_summary_tokens - refined_summary_tokens < CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS:
            if measure_request_tokens(refined_summary) <= input_budget_tokens:
                return refined_summary
            raise LLMException(message=ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED)

        summary = refined_summary
        refinement_index += 1
