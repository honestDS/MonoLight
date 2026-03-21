import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router as prompt_router
from app.api.v1.providers import router as provider_router
from app.api.v1.users import router as user_router
from app.core import constants
from app.core.exceptions import BaseBusinessException, LLMException
from app.providers.database import AsyncSessionLocal, Base, engine
from app.schemas.response import StandardResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时逻辑
    from sqlalchemy import select
    from app.models.profile import Profile
    from app.models.prompt import PromptLibrary

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        # 1. 初始化默认 Prompt
        prompt_res = await session.execute(select(PromptLibrary).where(PromptLibrary.name == "default"))
        prompt_obj = prompt_res.scalars().first()
        
        if not prompt_obj:
            default_prompt = PromptLibrary(
                name="default",
                content="",
                uid=None
            )
            session.add(default_prompt)
            await session.commit()
            await session.refresh(default_prompt)
            default_prompt_id = default_prompt.id
        else:
            default_prompt_id = prompt_obj.id

        # 2. 初始化默认 Profile
        profile_res = await session.execute(select(Profile).where(Profile.name == "default"))
        profile_obj = profile_res.scalars().first()
        
        if not profile_obj:
            default_profile = Profile(
                name="default",
                provider_id=-1,
                prompt_id=default_prompt_id,
                model_id="gemini-3-flash-preview",
                temperature=0.7,
                top_p=1.0,
                max_tokens=0,
                stream=False,
                extra_config={
                    "shell_timeout": 30
                },
                context_window_k=1024,
                is_active=True,
            )
            session.add(default_profile)
            await session.commit()
    yield


app = FastAPI(lifespan=lifespan, title="Monolight API", version="1.0.0")


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(
        status_code=500,
        content=StandardResponse.error(
            code=500, message=constants.ERR_DB_OPERATION_FAILED
        ).model_dump(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=StandardResponse.error(
            code=exc.status_code,
            message=(exc.detail if isinstance(exc.detail, str) else str(exc.detail)),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=StandardResponse.error(
            code=422, message=constants.ERR_VALIDATION_FAILED + ": " + str(exc.errors())
        ).model_dump(),
    )


@app.exception_handler(BaseBusinessException)
async def business_exception_handler(request: Request, exc: BaseBusinessException):
    if isinstance(exc, LLMException):
        import time

        ts = int(time.time())
        return JSONResponse(
            status_code=200,
            content={
                "id": f"chatcmpl-err-{ts}",
                "object": "chat.completion",
                "created": ts,
                "model": "monolight-v1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": exc.message},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            },
        )
    return JSONResponse(
        status_code=exc.code,
        content=StandardResponse.error(code=exc.code, message=exc.message).model_dump(),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=StandardResponse.error(
            code=500, message=constants.ERR_INTERNAL_SERVER_ERROR + ": " + str(exc)
        ).model_dump(),
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(provider_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(chat_router, prefix="/api/v1")
app.include_router(profile_router, prefix="/api/v1")
app.include_router(prompt_router, prefix="/api/v1")
app.include_router(user_router, prefix="/api/v1/admin", tags=["user_management"])


@app.get("/")
async def root():
    return {"status": "MonoLight is running"}


if __name__ == "__main__":
    port_env = os.getenv("APP_PORT")
    if not port_env:
        raise ValueError("APP_PORT must be set in .env file")
    port = int(port_env)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
