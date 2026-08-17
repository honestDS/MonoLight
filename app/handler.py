import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.constants import ERR_DB_OPERATION_FAILED, ERR_FAVICON_NOT_FOUND, ERR_INTERNAL_SERVER_ERROR, ERR_PASSWORD_TOO_LONG_BYTES, ERR_VALIDATION_FAILED
from app.core.crud.system_setting import system_setting_crud
from app.core.exceptions import BaseBusinessException, LLMException, ParameterException, ServerException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.i18n.locale import normalize_locale
from app.core.log import get_logger, reset_system_log_locale, set_system_log_locale
from app.core.paths import FAVICON_PATH
from app.providers.database import AsyncSessionLocal
from app.schemas.response import StandardResponse

logger = get_logger(__name__)


async def favicon():
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return JSONResponse(status_code=404, content=StandardResponse.error(code=404, message=ERR_FAVICON_NOT_FOUND).model_dump())


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    orig = getattr(exc, "orig", None)
    logger.error(
        "Database operation failed: method={} path={} sqlalchemy_error={} db_error={} db_message={}",
        request.method,
        request.url.path,
        type(exc).__name__,
        type(orig).__name__ if orig is not None else None,
        str(orig) if orig is not None else None,
    )
    return JSONResponse(status_code=500, content=StandardResponse.error(code=500, message=ERR_DB_OPERATION_FAILED).model_dump())


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content=StandardResponse.error(code=exc.status_code, message=detail).model_dump())


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_msgs = []
    message_key = ERR_VALIDATION_FAILED
    for error in exc.errors():
        field = error.get("loc")[-1]
        err_type = error.get("type")
        if err_type == ERR_PASSWORD_TOO_LONG_BYTES:
            message_key = ERR_PASSWORD_TOO_LONG_BYTES
        msg = t(err_type, default=err_type)
        if msg == err_type:
            msg = error.get("msg")
        error_msgs.append(f"[{field}] {msg}")

    validation_exc = ParameterException(message_key, code=422, detail=" | ".join(error_msgs))
    return JSONResponse(status_code=422, content=StandardResponse.error(code=422, message=validation_exc.message, detail=" | ".join(error_msgs)).model_dump())


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
                "choices": [{"index": 0, "message": {"role": "err", "content": exc.render_message()}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )
    return JSONResponse(status_code=exc.code, content=StandardResponse.from_exception(exc).model_dump())


async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception: method={} path={} error={}",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    server_exc = ServerException(message=ERR_INTERNAL_SERVER_ERROR, cause=str(exc))
    return JSONResponse(status_code=500, content=StandardResponse.from_exception(server_exc).model_dump())


async def locale_middleware(request: Request, call_next):
    locale_token = set_current_locale(normalize_locale(request.query_params.get("lang") or request.headers.get("Accept-Language")))
    log_locale_token = None
    try:
        async with AsyncSessionLocal() as db:
            settings = await system_setting_crud.get_runtime_settings(db)
            log_locale_token = set_system_log_locale(settings.log_locale)
        return await call_next(request)
    finally:
        if log_locale_token is not None:
            reset_system_log_locale(log_locale_token)
        reset_current_locale(locale_token)


async def root():
    return {"status": "MonoLight is running"}


def register_handlers(app: FastAPI) -> None:
    app.add_api_route("/favicon.ico", favicon, methods=["GET"], include_in_schema=False)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(BaseBusinessException, business_exception_handler)
    app.add_exception_handler(Exception, global_exception_handler)


def register_middlewares(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(locale_middleware)
