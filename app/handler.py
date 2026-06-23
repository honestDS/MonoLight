import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import constants
from app.core.crud.profile import profile_crud
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.i18n import t
from app.core.i18n.context import set_current_locale
from app.core.i18n.locale import normalize_locale
from app.core.log import reset_profile_log_locale, set_profile_log_locale
from app.core.paths import FAVICON_PATH
from app.providers.database import AsyncSessionLocal
from app.schemas.response import StandardResponse


async def favicon():
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return JSONResponse(status_code=404, content={"message": "Favicon not found"})


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(
        status_code=500,
        content=StandardResponse.error(code=500, message=constants.ERR_DB_OPERATION_FAILED).model_dump(),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=StandardResponse.error(
            code=exc.status_code,
            message=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        ).model_dump(),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_msgs = []
    for error in exc.errors():
        field = error.get("loc")[-1]
        err_type = error.get("type")
        # 如果错误类型在 validation 中找不到对应文案，则回退为 pydantic 原生的错误消息
        msg = t(err_type, default=err_type)
        if msg == err_type:
            msg = error.get("msg")

        error_msgs.append(f"[{field}] {msg}")

    return JSONResponse(
        status_code=422,
        content=StandardResponse.error(code=422, message=f"{t(constants.ERR_VALIDATION_FAILED, default=constants.ERR_VALIDATION_FAILED)}: " + " | ".join(error_msgs), raw_message=True).model_dump(),
    )


async def business_exception_handler(request: Request, exc: BaseBusinessException):
    if isinstance(exc, LLMException):
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
                        # LLM 异常单独使用 t 翻译
                        "message": {"role": "err", "content": t(exc.message, default=exc.message, **exc.kwargs)},
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
        content=StandardResponse.error(code=exc.code, message=exc.message, **exc.kwargs).model_dump(),
    )


async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=StandardResponse.error(code=500, message=t(constants.ERR_INTERNAL_SERVER_ERROR, default=constants.ERR_INTERNAL_SERVER_ERROR) + ": " + str(exc), raw_message=True).model_dump(),
    )


async def locale_middleware(request: Request, call_next):
    # HTTP 请求从 Accept-Language 读取语言；WebSocket 握手不经过 HTTP 中间件，
    # 其语言在对应的 WS handler 内通过 query 参数单独设置
    set_current_locale(normalize_locale(request.headers.get("Accept-Language")))
    log_locale_token = None
    try:
        async with AsyncSessionLocal() as db:
            log_locale_token = set_profile_log_locale(await profile_crud.get_active(db))
        return await call_next(request)
    finally:
        if log_locale_token is not None:
            reset_profile_log_locale(log_locale_token)


async def root():
    return {"status": "MonoLight is running"}


def register_handlers(app: FastAPI) -> None:
    app.add_api_route("/favicon.ico", favicon, methods=["GET"], include_in_schema=False)

    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(BaseBusinessException, business_exception_handler)
    app.add_exception_handler(Exception, global_exception_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(locale_middleware)
    app.add_api_route("/", root, methods=["GET"])
