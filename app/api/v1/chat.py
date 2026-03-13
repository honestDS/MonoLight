from fastapi import APIRouter, Depends
from app.core.security import get_current_user
from app.core.dispatcher import ChatDispatcher
from app.schemas.message import ChatCompletionRequest
from app.providers.database import AsyncSession, get_db
from app.transformers.openai import OpenAITransformer
import time

router = APIRouter(prefix='/chat', tags=['Chat'], dependencies=[Depends(get_current_user)])

@router.post('/completions')
async def chat_completions(request: ChatCompletionRequest, db: AsyncSession = Depends(get_db)):
    profile = await ChatDispatcher.dispatch(db, request.message)
    internal_response = {
        'id': f'chatcmpl-{int(time.time())}',
        'created': int(time.time()),
        'model': profile.model_id,
        'content': f'[Architecture OK] Active Profile: {profile.name}',
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    }
    return OpenAITransformer.from_standard(internal_response)