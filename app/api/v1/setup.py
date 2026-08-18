import asyncio

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.channels import (
    ChannelChatTestRequest,
    ChannelModelListRequest,
    list_channel_models,
    test_channel_chat,
)
from app.core.constants import (
    ERR_SETUP_ALREADY_COMPLETED,
    ERR_SETUP_NOT_ALLOWED,
    ERR_SETUP_STATUS_INVALID,
    ERR_SETUP_STATUS_NOT_INITIALIZED,
    MSG_SETUP_COMPLETE_SUCCESS,
    MSG_SETUP_STATUS_SUCCESS,
    SETUP_SESSION_COOKIE_NAME,
    SETUP_SESSION_COOKIE_PATH,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_PENDING,
)
from app.core.crud.system_setting import system_setting_crud
from app.core.exceptions import ParameterException, ServerException
from app.core.setup import complete_setup
from app.core.setup_session import establish_setup_session, require_setup_session
from app.core.system_secrets import SystemSecretsError, load_system_secrets
from app.providers.database import get_db
from app.schemas.response import StandardResponse
from app.schemas.setup import SetupCompleteRequest, SetupStatusData, SetupTokenData

router = APIRouter(prefix="/setup", tags=["Setup"])


def _setup_cookie_secure(request: Request) -> bool:
    return request.url.scheme.lower() == "https"


def _set_setup_cookie(response: Response, request: Request, token: str, max_age: int) -> None:
    response.set_cookie(
        key=SETUP_SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=SETUP_SESSION_COOKIE_PATH,
        secure=_setup_cookie_secure(request),
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_setup_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SETUP_SESSION_COOKIE_NAME,
        path=SETUP_SESSION_COOKIE_PATH,
        secure=_setup_cookie_secure(request),
        httponly=True,
        samesite="strict",
    )


async def _check_startup_integrity() -> None:
    try:
        await asyncio.to_thread(load_system_secrets)
    except SystemSecretsError as error:
        raise ServerException(error.message_key, params=error.params) from None


async def _get_valid_setup_status(db: AsyncSession) -> str:
    status = await system_setting_crud.get_setup_status(db)
    if status is None:
        raise ServerException(ERR_SETUP_STATUS_NOT_INITIALIZED)
    if status not in {SETUP_STATUS_PENDING, SETUP_STATUS_CONFIGURING, SETUP_STATUS_COMPLETED}:
        raise ServerException(ERR_SETUP_STATUS_INVALID)
    return status


async def _check_setup_pending(request: Request, db: AsyncSession) -> None:
    await _check_startup_integrity()
    status = await _get_valid_setup_status(db)

    if status == SETUP_STATUS_COMPLETED:
        raise ParameterException(ERR_SETUP_ALREADY_COMPLETED, code=409)
    if status == SETUP_STATUS_CONFIGURING:
        raise ParameterException(ERR_SETUP_NOT_ALLOWED, code=409)
    await require_setup_session(db, request.cookies.get(SETUP_SESSION_COOKIE_NAME))


@router.get(
    "/status",
    response_model=StandardResponse[SetupStatusData],
    responses={
        403: {"model": StandardResponse},
        409: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def get_setup_status(
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> StandardResponse[SetupStatusData]:
    await _check_startup_integrity()
    status = await _get_valid_setup_status(db)

    if status == SETUP_STATUS_CONFIGURING:
        raise ParameterException(ERR_SETUP_NOT_ALLOWED, code=409)
    if status == SETUP_STATUS_PENDING:
        issued = await establish_setup_session(db, http_request.cookies.get(SETUP_SESSION_COOKIE_NAME))
        if issued is not None:
            _set_setup_cookie(response, http_request, issued.token, issued.max_age)
    else:
        _clear_setup_cookie(response, http_request)

    return StandardResponse.success(
        data=SetupStatusData(required=status == SETUP_STATUS_PENDING),
        message=MSG_SETUP_STATUS_SUCCESS,
    )


@router.post(
    "/models",
    response_model=StandardResponse,
    responses={
        403: {"model": StandardResponse},
        409: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def list_setup_models(
    payload: ChannelModelListRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> StandardResponse:
    await _check_setup_pending(http_request, db)
    return await list_channel_models(payload=payload, _admin={})


@router.post(
    "/test-chat",
    response_model=StandardResponse,
    responses={
        403: {"model": StandardResponse},
        409: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def test_setup_chat(
    payload: ChannelChatTestRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> StandardResponse:
    await _check_setup_pending(http_request, db)
    return await test_channel_chat(payload=payload, _admin={})


@router.post(
    "/complete",
    response_model=StandardResponse[SetupTokenData],
    responses={
        403: {"model": StandardResponse},
        409: {"model": StandardResponse},
        422: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def complete_setup_request(
    payload: SetupCompleteRequest,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> StandardResponse[SetupTokenData]:
    await _check_startup_integrity()
    status = await _get_valid_setup_status(db)

    if status == SETUP_STATUS_COMPLETED:
        raise ParameterException(ERR_SETUP_ALREADY_COMPLETED, code=409)
    if status == SETUP_STATUS_CONFIGURING:
        raise ParameterException(ERR_SETUP_NOT_ALLOWED, code=409)

    await require_setup_session(db, http_request.cookies.get(SETUP_SESSION_COOKIE_NAME))
    result = await complete_setup(db, payload)
    _clear_setup_cookie(response, http_request)
    return StandardResponse.success(
        data=SetupTokenData(
            access_token=result.access_token,
            token_type=result.token_type,
            profile_id=result.profile_id,
            channel_id=result.channel_id,
        ),
        message=MSG_SETUP_COMPLETE_SUCCESS,
    )
