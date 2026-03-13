import aiohttp, json, logging
from app.models.profile import Profile
logger = logging.getLogger(__name__)

class LLMClient:
    @staticmethod
    async def generate(profile: Profile, messages: list):
        provider = profile.provider
        headers = {'Authorization': f'Bearer {provider.api_key}', 'Content-Type': 'application/json'}
        payload = {
            'model': profile.model_id,
            'messages': messages,
            'temperature': profile.temperature,
            'stream': False
        }
        # 容错处理：若 max_tokens > 0 则带入参数，否则不传递该键以规避 API 报错
        if profile.max_tokens and profile.max_tokens > 0:
            payload['max_tokens'] = profile.max_tokens

        base_url = provider.base_url.rstrip('/')
        url = f'{base_url}/chat/completions'
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                txt = await resp.text()
                if resp.status != 200: raise Exception(f'Error {resp.status}: {txt}')
                return json.loads(txt)