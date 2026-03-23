import logging
from typing import Any, Dict, List, Optional
from app.transformers.openai import OpenAITransformer
from app.schemas.message import InternalMessage, InternalResponse

logger = logging.getLogger(__name__)


class LLMClient:
    _transformers = {"openai": OpenAITransformer()}

    @classmethod
    async def generate(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: List[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        **kwargs,
    ) -> InternalResponse:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            from app.core.exceptions import LLMException
            from app.core import constants

            raise LLMException(
                f"{constants.ERR_LLM_UNEXPECTED_ERROR}: Unsupported protocol {protocol}"
            )

        return await transformer.generate(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
