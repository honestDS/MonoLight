import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import (
    FastAPI,
    Request,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.files import router as files_router
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router as prompt_router
from app.api.v1.providers import router as provider_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as user_router
from app.core import constants
from app.core.exceptions import (
    BaseBusinessException,
    LLMException,
)
from app.core.log import LogManager
from app.providers.database import AsyncSessionLocal
from app.schemas.response import StandardResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.utils.dt import get_local_time
    from app.providers.init_db import init_system_data

    async with AsyncSessionLocal() as session:
        await init_system_data(session)

    # 记录启动时的信息，确保此时异步环境已就绪，日志能够入库
    now_aware = get_local_time()
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", "data/logs/monolight.log"),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    from app.core.log import get_logger

    get_logger("app.core.log").info(f"Log system initialized. Path: {os.getenv('LOG_FILE_PATH', 'data/logs/monolight.log')} | Time: {now_aware.isoformat()}")

    yield


app = FastAPI(lifespan=lifespan, title="Monolight API", version="1.0.0")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path = "/www/server/python_project/Monobot/dashboard/public/favicon.ico"
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse(status_code=404, content={"message": "Favicon not found"})


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(
        status_code=500,
        content=StandardResponse.error(code=500, message=constants.ERR_DB_OPERATION_FAILED).model_dump(),
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
    error_msgs = []
    for error in exc.errors():
        field = error.get("loc")[-1]
        err_type = error.get("type")
        msg = constants.ERR_MAP.get(err_type, error.get("msg"))
        error_msgs.append(f"[{field}] {msg}")

    return JSONResponse(
        status_code=422,
        content=StandardResponse.error(code=422, message=f"{constants.ERR_VALIDATION_FAILED}: " + " | ".join(error_msgs)).model_dump(),
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
                        "message": {"role": "err", "content": exc.message},
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
        content=StandardResponse.error(code=500, message=constants.ERR_INTERNAL_SERVER_ERROR + ": " + str(exc)).model_dump(),
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 使用 APIRouter 内部定义的 tags，避免在 include_router 时重复或冲突定义
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(user_router, prefix="/api/v1/admin")
app.include_router(provider_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(chat_router, prefix="/api/v1")
app.include_router(files_router, prefix="/api/v1", tags=["Files"])
app.include_router(profile_router, prefix="/api/v1")
app.include_router(prompt_router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"status": "MonoLight is running"}


if __name__ == "__main__":
    port_env = os.getenv("APP_PORT")
    if not port_env:
        raise ValueError("APP_PORT must be set in .env file")
    port = int(port_env)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
