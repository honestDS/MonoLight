"""Profile API：渠道管理架构适配版

CRUD 支持 chat_channel/embedding_channel/rerank_channel/image_generation_channel；activate 校验适配
"""

from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.channel import channel_crud
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.models.channel import ChannelConfig, ModelUsage
from app.models.profile import (
    ProfileConfig,
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
)
from app.providers.database import get_db
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(
    prefix="/profiles",
    tags=["Profile Management"],
    dependencies=[Depends(get_current_user)],
)


PROFILE_TOOL_OPTIONS = [
    {"value": "execute_shell", "label_key": "TOOL_EXECUTE_SHELL_LABEL"},
    {"value": "write_file", "label_key": "TOOL_WRITE_FILE_LABEL"},
    {"value": "firecrawl_search", "label_key": "TOOL_FIRECRAWL_SEARCH_LABEL"},
    {"value": "firecrawl_scrape", "label_key": "TOOL_FIRECRAWL_SCRAPE_LABEL"},
    {"value": "send_file_to_user", "label_key": "TOOL_SEND_FILE_TO_USER_LABEL"},
    {"value": "list_background_tasks", "label_key": "TOOL_LIST_BACKGROUND_TASKS_LABEL"},
    {"value": "cancel_background_task", "label_key": "TOOL_CANCEL_BACKGROUND_TASK_LABEL"},
    {"value": "generate_image", "label_key": "TOOL_GENERATE_IMAGE_LABEL"},
    {"value": "query_knowledge_base", "label_key": "TOOL_QUERY_KNOWLEDGE_BASE_LABEL"},
]


def get_profile_tool_options() -> list[dict[str, str]]:
    return [{"value": item["value"], "label": t(item["label_key"], default=item["value"])} for item in PROFILE_TOOL_OPTIONS]


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


async def validate_audit_model_config(db: AsyncSession, security_config: dict) -> None:
    """校验安全审计模型必须指向渠道下的 CHAT 模型。"""
    audit_channel_id = security_config.get("audit_channel_id")
    audit_model_id = security_config.get("audit_model_id")
    if not audit_channel_id or not audit_model_id:
        return

    channel = await channel_crud.get(db, audit_channel_id)
    if not channel:
        raise ParameterException(constants.ERR_PROFILE_AUDIT_MODEL_NOT_CHAT)

    is_chat_model = any(item.get("model_id") == audit_model_id and item.get("usage") == ModelUsage.CHAT for item in (channel.model_ids or []))
    if not is_chat_model:
        raise ParameterException(constants.ERR_PROFILE_AUDIT_MODEL_NOT_CHAT)


async def validate_channel_rule_usage(db: AsyncSession, channel_config_obj: ChannelConfig, expected_usage: ModelUsage) -> None:
    """校验渠道规则引用的模型条目用途与所在配置组一致。"""
    for rule in channel_config_obj.rules:
        channel = await channel_crud.get(db, rule.channel_id)
        if not channel:
            raise ParameterException(constants.ERR_CHANNEL_NOT_FOUND)

        matched_model = None
        for item in channel.model_ids or []:
            if str(item.get("model_id")) == rule.model_id:
                matched_model = item
                if str(item.get("usage")) == expected_usage.value:
                    break

        if not matched_model:
            raise ParameterException(constants.ERR_CHANNEL_MODEL_NOT_FOUND)

        actual_usage = str(matched_model.get("usage"))
        if actual_usage != expected_usage.value:
            raise ParameterException(constants.ERR_CHANNEL_USAGE_MISMATCH, expected=expected_usage.value, actual=actual_usage)


async def validate_channel_configs(db: AsyncSession, channel_config: dict) -> None:
    """校验 channel 配置中的渠道配置合法性。

    - chat_channel：至少需要一条启用规则
    - embedding_channel：可选，有配置则校验
    - rerank_channel：可选，有配置则校验
    - image_generation_channel：可选，有配置则校验
    - 每条规则必须引用对应用途的模型条目
    """
    channel_usage_map = {
        "chat_channel": ModelUsage.CHAT,
        "embedding_channel": ModelUsage.EMBEDDING,
        "rerank_channel": ModelUsage.RERANK,
        "image_generation_channel": ModelUsage.IMAGE_GENERATION,
    }

    for channel_key, expected_usage in channel_usage_map.items():
        channel_raw = channel_config.get(channel_key)
        if not channel_raw:
            continue

        try:
            channel_config_obj = ChannelConfig.model_validate(channel_raw)
        except Exception as e:
            raise ParameterException(f"{channel_key} 校验失败: {e}")

        await validate_channel_rule_usage(db, channel_config_obj, expected_usage)

        if expected_usage == ModelUsage.RERANK and channel_config_obj.rerank_candidate_k < channel_config_obj.kb_query_top_k:
            raise ParameterException(constants.ERR_PROFILE_RERANK_CANDIDATE_K_TOO_SMALL)


@router.post("/create", response_model=StandardResponse[ProfileResponse])
async def create_profile(
    profile_in: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    profile_in.configs = ProfileConfig.model_validate(profile_in.configs).model_dump()
    channel_config = profile_in.configs.get("channel", {})
    await validate_channel_configs(db, channel_config)
    await validate_audit_model_config(db, profile_in.configs.get("security", {}))

    if await profile_crud.get_by_name(db, profile_in.name):
        raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ParameterException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.create(db, obj_in=profile_in)
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    return StandardResponse.success(
        data=res_data,
        message=constants.MSG_PROFILE_CREATED,
    )


@router.get("/list", response_model=StandardResponse)
async def list_profiles(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    skip = (page - 1) * size
    profiles = await profile_crud.get_multi(db, skip=skip, limit=size)
    total = await profile_crud.count(db)

    results = []
    for p in profiles:
        item = ProfileResponse.model_validate(p)
        results.append(item)

    page_data = PageData(
        items=results,
        total=total,
        page=page,
        size=size,
        meta={"tool_options": get_profile_tool_options()},
    )
    return StandardResponse.success(data=page_data)


@router.post("/activate")
async def activate_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    profile = await profile_crud.get(db, profile_id)
    if not profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    # 校验 chat_channel 配置存在
    channel_config = profile.configs.get("channel", {})
    chat_channel = channel_config.get("chat_channel")
    if not chat_channel or not chat_channel.get("rules"):
        raise ParameterException(constants.ERR_ACTIVATE_NO_CHANNEL)
    await validate_channel_configs(db, channel_config)

    await db.execute(update(profile_crud.model).values(is_active=False))
    profile.is_active = True
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROFILE_ACTIVATED)


@router.post("/update", response_model=StandardResponse[ProfileResponse])
async def update_profile(
    profile_id: int,
    profile_in: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    if profile_in.configs:
        profile_in.configs = ProfileConfig.model_validate(profile_in.configs).model_dump()
        channel_config = profile_in.configs.get("channel", {})
        await validate_channel_configs(db, channel_config)
        await validate_audit_model_config(db, profile_in.configs.get("security", {}))

    if profile_in.name and profile_in.name != db_profile.name:
        if await profile_crud.get_by_name(db, profile_in.name):
            raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.update(db, db_obj=db_profile, obj_in=profile_in)
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    return StandardResponse.success(
        data=res_data,
        message=constants.MSG_PROFILE_UPDATED,
    )


@router.post("/delete")
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    count = len(await profile_crud.get_multi(db))
    if count <= 1:
        raise ParameterException(constants.ERR_DELETE_LAST_PROFILE)

    if db_profile.is_active:
        raise ParameterException(constants.ERR_DELETE_ACTIVE_PROFILE)

    await profile_crud.remove(db, id=profile_id)
    return StandardResponse.success(message=constants.MSG_PROFILE_DELETED)
