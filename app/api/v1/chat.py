from fastapi import APIRouter, Depends, HTTPException
from app.core.security import get_current_user
from app.transformers.openai import OpenAITransformer
from app.schemas.message import ChatCompletionRequest
from app.providers.database import AsyncSession, get_db
from app.core.dispatcher import ChatDispatcher
import time

router = APIRouter(prefix='/chat', tags=['Chat'], dependencies=[Depends(get_current_user)])

@router.post('/completions')
async def chat_completions(request: ChatCompletionRequest, db: AsyncSession = Depends(get_db)):
    # 通过分发器获取后端配置
    messages = [{'role': 'user', 'content': request.message}]
    config, profile = await ChatDispatcher.dispatch(db, messages)

    # 模拟 AI 响应内容（后续接入 Adapter）
    internal_response = {
        'id': f'chatcmpl-{int(time.time())}',
        'created': int(time.time()),
        'model': config['model'],
        'content': f'已通过配置【{profile.name}】处理，使用模型：{config['model']}',
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    }

    return OpenAITransformer.from_standard(internal_response)