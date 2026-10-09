import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.constants import (
    ERR_DB_OPERATION_FAILED,
    ERR_FAVICON_NOT_FOUND,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_REQUEST_VALIDATION_FAILED,
    ERR_USERNAME_FORMAT,
    ERR_VALIDATION_FAILED,
    MSG_VALIDATION_ERROR_SEPARATOR,
    MSG_VALIDATION_FIELD_ERROR,
    MSG_VALIDATION_PASSWORD,
    MSG_VALIDATION_REQUEST,
    MSG_VALIDATION_USERNAME,
)
from app.core.crud.system.setting import system_setting_crud
from app.core.exceptions import BaseBusinessException, LLMException, ServerException
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
    field_labels = {"username": MSG_VALIDATION_USERNAME, "password": MSG_VALIDATION_PASSWORD}
    for error in exc.errors():
        loc = error.get("loc") or ()
        if loc and loc[0] in {"body", "query", "path", "header", "cookie"}:
            loc = loc[1:]
        err_type = error.get("type") or ""
        ctx = error.get("ctx") if isinstance(error.get("ctx"), dict) else {}
        field = t(MSG_VALIDATION_REQUEST) if not loc or err_type == "json_invalid" else ".".join(t(field_labels[part]) if part in field_labels else str(part) for part in loc)

        if err_type == "string_pattern_mismatch" and loc and str(loc[-1]) == "username":
            reason = t(ERR_USERNAME_FORMAT)
        elif err_type == "value_error" and "error" in ctx:
            reason = t(str(ctx["error"]), default=str(ctx["error"]))
        else:
            reason = t(err_type, default=error.get("msg") or t(ERR_VALIDATION_FAILED), **ctx)
        error_msgs.append(t(MSG_VALIDATION_FIELD_ERROR, field=field, error=reason))

    if error_msgs:
        detail = t(MSG_VALIDATION_ERROR_SEPARATOR).join(error_msgs)
        response = StandardResponse.error(code=422, message=ERR_REQUEST_VALIDATION_FAILED, detail=detail)
    else:
        response = StandardResponse.error(code=422, message=ERR_VALIDATION_FAILED)
    return JSONResponse(status_code=422, content=response.model_dump())


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
        allow_origins=[],
        allow_origin_regex=r"^https?://.+$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(locale_middleware)
