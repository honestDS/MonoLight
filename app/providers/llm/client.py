import json
import logging

import aiohttp

from app.core import constants
from app.core.exceptions import LLMException

logger = logging.getLogger(__name__)


class LLMClient:
    @staticmethod
    async def generate(
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list,
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list = None,
        tool_choice: str = "auto",
    ):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = max_tokens

        url = f"{base_url.rstrip('/')}/chat/completions"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(f"{constants.ERR_LLM_API_RESPONSE_ERROR} [Status: {resp.status}]: {txt}")
                    return json.loads(txt)
        except aiohttp.ClientConnectorError as e:
            logger.error(f"LLM Connection Error: {str(e)}")
            raise LLMException(f"{constants.ERR_LLM_CONNECTION_FAILED}: {str(e)}")
        except LLMException as e:
            raise e
        except Exception as e:
            logger.error(f"LLM Unexpected Error: {str(e)}")
            raise LLMException(f"{constants.ERR_LLM_UNEXPECTED_ERROR}: {str(e)}")
