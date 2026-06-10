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
    ModelUsage,
    ProviderCreate,
    ProviderResponse,
    ProviderType,
    ProviderUpdate,
)
from app.providers.database import get_db
from app.providers.embedding import EmbeddingClient
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(prefix="/providers", tags=["Providers"], dependencies=[Depends(get_current_user)])


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

    db_obj = await provider_crud.create(db, obj_in=provider_in)

    return StandardResponse.success(
        data=ProviderResponse.model_validate(db_obj),
        message=constants.MSG_PROVIDER_CREATED,
    )


@router.get("/types", response_model=StandardResponse)
async def get_provider_types():
    return StandardResponse.success(
        data={
            "provider_types": [e.value for e in ProviderType],
            "model_usages": [e.value for e in ModelUsage],
        }
    )


@router.get("/list", response_model=StandardResponse)
async def list_providers(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    skip = (page - 1) * size
    providers = await provider_crud.get_multi(db, skip=skip, limit=size)
    total = await provider_crud.count(db)

    page_data = PageData(
        items=[ProviderResponse.model_validate(item) for item in providers],
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(data=page_data)


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

    # 跨字段校验：结合库内既有 usage 判断更新后是否仍满足 RERANK 的 base_url 必填约束
    # ProviderUpdate 为部分更新模型，usage 可能为 None（本次未改 usage），需结合数据库既有记录判断
    final_usage = provider_in.usage if provider_in.usage is not None else db_obj.usage
    final_base_url = provider_in.base_url if provider_in.base_url is not None else db_obj.base_url
    if final_usage == ModelUsage.RERANK and not final_base_url:
        raise ParameterException("usage 为 RERANK 时 base_url 必须配置")

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


@router.post("/test-embedding-dimension")
async def test_embedding_dimension(
    provider_id: int,
    model_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    """
    自动检测向量模型的输出维度。
    通过向大模型发送一条测试文本，并提取返回结果中向量的长度。
    """
    db_obj = await provider_crud.get(db, provider_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)

    if not db_obj.base_url:
        raise ParameterException("该提供商未配置 Base URL，无法执行自动检测。")

    try:
        res = await EmbeddingClient.get_embeddings(
            provider_type=db_obj.provider_type,
            api_key=db_obj.api_key,
            base_url=db_obj.base_url,
            model_id=model_id,
            input_texts=["dimension test"],
        )
        if "data" in res and len(res["data"]) > 0:
            dim = len(res["data"][0]["embedding"])
            return StandardResponse.success(
                data={"dimension": dim},
                message=f"检测成功，该模型的默认输出维度为: {dim}",
            )
        else:
            raise ParameterException("模型返回的数据结构异常，无法获取维度。")
    except Exception as e:
        raise ParameterException(f"检测失败: {str(e)}")
