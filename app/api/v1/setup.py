import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_SETUP_ALREADY_COMPLETED,
    ERR_SETUP_NOT_ALLOWED,
    ERR_SETUP_STATUS_INVALID,
    ERR_SETUP_STATUS_NOT_INITIALIZED,
    MSG_SETUP_COMPLETE_SUCCESS,
    MSG_SETUP_STATUS_SUCCESS,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_PENDING,
)
from app.core.crud.system_setting import system_setting_crud
from app.core.exceptions import ParameterException, ServerException
from app.core.setup import complete_setup
from app.core.system_secrets import SystemSecretsError, load_system_secrets
from app.providers.database import get_db
from app.schemas.response import StandardResponse
from app.schemas.setup import SetupCompleteRequest, SetupStatusData, SetupTokenData

router = APIRouter(prefix="/setup", tags=["Setup"])


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


@router.get(
    "/status",
    response_model=StandardResponse[SetupStatusData],
    responses={
        409: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def get_setup_status(db: AsyncSession = Depends(get_db)) -> StandardResponse[SetupStatusData]:
    await _check_startup_integrity()
    status = await _get_valid_setup_status(db)

    if status == SETUP_STATUS_CONFIGURING:
        raise ParameterException(ERR_SETUP_NOT_ALLOWED, code=409)

    return StandardResponse.success(
        data=SetupStatusData(required=status == SETUP_STATUS_PENDING),
        message=MSG_SETUP_STATUS_SUCCESS,
    )


@router.post(
    "/complete",
    response_model=StandardResponse[SetupTokenData],
    responses={
        409: {"model": StandardResponse},
        422: {"model": StandardResponse},
        500: {"model": StandardResponse},
    },
)
async def complete_setup_request(
    request: SetupCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> StandardResponse[SetupTokenData]:
    await _check_startup_integrity()
    status = await _get_valid_setup_status(db)

    if status == SETUP_STATUS_COMPLETED:
        raise ParameterException(ERR_SETUP_ALREADY_COMPLETED, code=409)
    if status == SETUP_STATUS_CONFIGURING:
        raise ParameterException(ERR_SETUP_NOT_ALLOWED, code=409)

    result = await complete_setup(db, request)
    return StandardResponse.success(
        data=SetupTokenData(
            access_token=result.access_token,
            token_type=result.token_type,
        ),
        message=MSG_SETUP_COMPLETE_SUCCESS,
    )
