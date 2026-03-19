from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.database import get_db
from app.schemas.provider import ProviderCreate, ProviderRead, ProviderUpdate
from app.models.provider import ModelProvider
from app.core.security import get_current_user
from app.schemas.response import StandardResponse
from app.core import constants
from app.core.exceptions import ResourceNotFoundException, ParameterException
from sqlalchemy import select

router = APIRouter(
    prefix="/providers", tags=["Providers"], dependencies=[Depends(get_current_user)]
)


@router.post("/create", response_model=StandardResponse)
async def create_provider(
    provider_in: ProviderCreate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ModelProvider).where(ModelProvider.name == provider_in.name)
    )
    if result.scalar_one_or_none():
        raise ParameterException(constants.ERR_PROVIDER_NAME_EXISTS)
    db_obj = ModelProvider(**provider_in.model_dump())
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return StandardResponse.success(
        ProviderRead.model_validate(db_obj), message=constants.MSG_PROVIDER_CREATED
    )


@router.get("/list", response_model=StandardResponse)
async def list_providers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider))
    return StandardResponse.success(
        [ProviderRead.model_validate(item) for item in result.scalars().all()]
    )


@router.get("/get", response_model=StandardResponse)
async def get_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ModelProvider).where(ModelProvider.id == provider_id)
    )
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)
    return StandardResponse.success(ProviderRead.model_validate(db_obj))


@router.post("/update", response_model=StandardResponse)
async def update_provider(
    provider_id: int, provider_in: ProviderUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ModelProvider).where(ModelProvider.id == provider_id)
    )
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)

    for field, value in provider_in.model_dump(exclude_unset=True).items():
        setattr(db_obj, field, value)
    await db.commit()
    await db.refresh(db_obj)
    return StandardResponse.success(
        ProviderRead.model_validate(db_obj), message=constants.MSG_PROVIDER_UPDATED
    )


@router.post("/delete")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ModelProvider).where(ModelProvider.id == provider_id)
    )
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)
    await db.delete(db_obj)
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROVIDER_DELETED)
