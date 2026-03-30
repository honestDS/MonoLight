from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.provider import provider_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user
from app.models.provider import (
    ProviderCreate,
    ProviderResponse,
    ProviderType,
    ProviderUpdate,
)
from app.providers.database import get_db
from app.schemas.response import StandardResponse

router = APIRouter(
    prefix="/providers", tags=["Providers"], dependencies=[Depends(get_current_user)]
)


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


@router.post("/create", response_model=StandardResponse)
async def create_provider(
    provider_in: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    if await provider_crud.get_by_name(db, provider_in.name):
        raise ParameterException(constants.ERR_PROVIDER_NAME_EXISTS)

    provider_in.is_active = True # 该参数暂不允许设置
    db_obj = await provider_crud.create(db, obj_in=provider_in)
    return StandardResponse.success(
        data=ProviderResponse.model_validate(db_obj),
        message=constants.MSG_PROVIDER_CREATED,
    )


@router.get("/types", response_model=StandardResponse)
async def get_provider_types():
    return StandardResponse.success(data=[e.value for e in ProviderType])

@router.get("/list", response_model=StandardResponse)
async def list_providers(db: AsyncSession = Depends(get_db)):
    providers = await provider_crud.get_multi(db)
    return StandardResponse.success(
        data=[ProviderResponse.model_validate(item) for item in providers]
    )


@router.get("/get", response_model=StandardResponse)
async def get_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    db_obj = await provider_crud.get(db, provider_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)
    return StandardResponse.success(data=ProviderResponse.model_validate(db_obj))


@router.post("/update", response_model=StandardResponse)
async def update_provider(
    provider_id: int,
    provider_in: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_obj = await provider_crud.get(db, provider_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)

    if provider_in.name and provider_in.name != db_obj.name:
        if await provider_crud.get_by_name(db, provider_in.name):
            raise ParameterException(constants.ERR_PROVIDER_NAME_EXISTS)

    provider_in.is_active = True  # 该参数暂不允许设置
    db_obj = await provider_crud.update(db, db_obj=db_obj, obj_in=provider_in)
    return StandardResponse.success(
        data=ProviderResponse.model_validate(db_obj),
        message=constants.MSG_PROVIDER_UPDATED,
    )


@router.post("/delete")
async def delete_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_obj = await provider_crud.get(db, provider_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)
    await provider_crud.remove(db, id=provider_id)
    return StandardResponse.success(message=constants.MSG_PROVIDER_DELETED)
