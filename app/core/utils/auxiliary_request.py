from app.core.constants import ERR_AUXILIARY_REQUEST_OUTPUT_BUDGET_EXHAUSTED
from app.core.exceptions import LLMException
from app.models.message import InternalResponse


class AuxiliaryRequestOutputBudgetError(LLMException):
    def __init__(self, message: str = ERR_AUXILIARY_REQUEST_OUTPUT_BUDGET_EXHAUSTED, **kwargs):
        super().__init__(message=message, **kwargs)


def ensure_auxiliary_output_budget(response: InternalResponse, *, max_output_tokens: int) -> None:
    finish_reason = response.finish_reason
    if finish_reason == "length":
        raise AuxiliaryRequestOutputBudgetError()

    completion_tokens = (response.usage or {}).get("completion_tokens")
    if finish_reason in {None, "incomplete"} and max_output_tokens > 0 and isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool) and completion_tokens >= max_output_tokens:
        raise AuxiliaryRequestOutputBudgetError()
