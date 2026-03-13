import aiohttp
import json
import logging
from app.models.profile import Profile

logger = logging.getLogger(__name__)

class LLMClient:
    @staticmethod
    async def generate(profile: Profile, message: str):
        provider = profile.provider
        headers = {
            'Authorization': f'Bearer {provider.api_key}',
            'Content-Type': 'application/json'
        }
        payload = {
            'model': profile.model_id,
            'messages': [{'role': 'user', 'content': message}],
            'temperature': profile.temperature,
            'max_tokens': profile.max_tokens,
            'stream': False
        }
        
        # 修复 URL 拼接中可能出现的多余斜杠问题
        base_url = provider.base_url.rstrip('/')
        url = f'{base_url}/chat/completions'
        
        logger.info(f'Requesting LLM: {url}')
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    logger.error(f'LLM Error {resp.status}: {response_text}')
                    raise Exception(f'LLM Provider Error: {resp.status} - {response_text}')
                
                try:
                    return json.loads(response_text)
                except json.JSONDecodeError:
                    logger.error(f'Failed to decode JSON. Raw response: {response_text}')
                    raise Exception(f'LLM returned non-JSON response: {response_text[:200]}')