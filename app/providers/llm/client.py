import aiohttp
import json
from app.models.profile import Profile

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
        
        url = f'{provider.base_url}/chat/completions'
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f'LLM Provider Error: {resp.status} - {error_text}')
                return await resp.json()