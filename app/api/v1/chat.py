from fastapi import APIRouter, Depends
from app.core.security import get_current_user
from app.core.dispatcher import ChatDispatcher
from app.schemas.message import ChatCompletionRequest
from app.providers.database import AsyncSession, get_db
from app.transformers.openai import OpenAITransformer

router = APIRouter(prefix='/chat', tags=['Chat'], dependencies=[Depends(get_current_user)])

@router.post('/completions')
async def chat_completions(request: ChatCompletionRequest, db: AsyncSession = Depends(get_db)):
    # 异步等待调度器返回真实推理结果
    llm_response = await ChatDispatcher.dispatch(db, request.message)
    # 格式化输出
    return OpenAITransformer.from_standard(llm_response)