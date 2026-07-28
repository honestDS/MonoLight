"""Profile API：渠道管理架构适配版

CRUD 支持对话、上下文总结、重排和图像生成渠道；default 校验适配
"""

from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_DELETE_BOUND_PROFILE,
    ERR_DELETE_DEFAULT_PROFILE,
    ERR_DELETE_LAST_PROFILE,
    ERR_KB_NOT_FOUND,
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_PROFILE_NAME_EXISTS,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROMPT_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
    MSG_PROFILE_CREATED,
    MSG_PROFILE_DELETED,
    MSG_PROFILE_SET_DEFAULT,
    MSG_PROFILE_UPDATED,
)
from app.core.crud.message_platform import message_platform_crud
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.crud.session import session_crud
from app.core.crud.user import user_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.i18n import t
from app.core.profile_validation import (
    validate_audit_model_config,
    validate_channel_configs,
    validate_profile_for_assignment,
)
from app.core.security import get_current_user
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding
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


async def get_profile_knowledge_base_ids(db: AsyncSession, profile_id: int, uid: str | None) -> list[int]:
    result = await db.execute(select(KnowledgeBaseProfileBinding.knowledge_base_id).join(KnowledgeBase, KnowledgeBase.id == KnowledgeBaseProfileBinding.knowledge_base_id).where(KnowledgeBaseProfileBinding.profile_id == profile_id).where(KnowledgeBase.uid == uid))
    return list(result.scalars().all())


async def replace_profile_knowledge_base_bindings(db: AsyncSession, profile_id: int, uid: str | None, knowledge_base_ids: list[int] | None) -> None:
    if knowledge_base_ids is None:
        return
    normalized_kb_ids = list(dict.fromkeys(knowledge_base_ids))
    if normalized_kb_ids:
        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id.in_(normalized_kb_ids)).where(KnowledgeBase.uid == uid))
        knowledge_bases = list(result.scalars().all())
        if len(knowledge_bases) != len(normalized_kb_ids):
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

    existing_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.profile_id == profile_id))
    for binding in existing_result.scalars().all():
        kb = await db.get(KnowledgeBase, binding.knowledge_base_id)
        if kb and kb.uid == uid:
            await db.delete(binding)
    for kb_id in normalized_kb_ids:
        db.add(KnowledgeBaseProfileBinding(knowledge_base_id=kb_id, profile_id=profile_id))


async def build_profile_response(db: AsyncSession, profile: object, *, username: str | None = None) -> ProfileResponse:
    item = ProfileResponse.model_validate(profile)
    if username is not None:
        item.username = username
    if item.id is not None:
        item.knowledge_base_ids = await get_profile_knowledge_base_ids(db, item.id, item.uid)
    return item


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
    return current_user


@router.post("/create", response_model=StandardResponse[ProfileResponse])
async def create_profile(
    profile_in: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if getattr(current_user, "is_superuser", False):
        profile_in.uid = profile_in.uid or current_user.uid
    elif profile_in.uid and profile_in.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
    else:
        profile_in.uid = current_user.uid
    profile_in.configs = ProfileConfig.model_validate(profile_in.configs).model_dump()
    channel_config = profile_in.configs.get("channel", {})
    await validate_channel_configs(db, channel_config)
    await validate_audit_model_config(db, profile_in.configs.get("security", {}))

    if await profile_crud.get_by_name(db, profile_in.name, uid=profile_in.uid):
        raise ParameterException(ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get_visible(db, profile_in.prompt_id, uid=profile_in.uid):
            raise ParameterException(ERR_PROMPT_NOT_FOUND)

    knowledge_base_ids = profile_in.knowledge_base_ids
    db_profile = await profile_crud.create(db, obj_in=profile_in.model_dump(exclude={"knowledge_base_ids"}))
    await replace_profile_knowledge_base_bindings(db, db_profile.id, db_profile.uid, knowledge_base_ids)
    await db.commit()
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = await build_profile_response(db, db_profile)
    return StandardResponse.success(
        data=res_data,
        message=MSG_PROFILE_CREATED,
    )


@router.get("/list", response_model=StandardResponse)
async def list_profiles(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    skip = (page - 1) * size
    is_admin = getattr(current_user, "is_superuser", False)
    if is_admin:
        profiles = await profile_crud.get_multi_all(db, skip=skip, limit=size)
        total = await profile_crud.count_all(db)
    else:
        profiles = await profile_crud.get_multi(db, skip=skip, limit=size, uid=current_user.uid)
        total = await profile_crud.count(db, uid=current_user.uid)

    user_map = {}
    if is_admin:
        profile_uids = list({profile.uid for profile in profiles if profile.uid})
        users = await user_crud.get_multi_by_uids(db, profile_uids)
        user_map = {user.uid: user.username for user in users}

    results = []
    for p in profiles:
        item = await build_profile_response(db, p, username=user_map.get(p.uid) if is_admin else None)
        results.append(item)

    page_data = PageData(
        items=results,
        total=total,
        page=page,
        size=size,
        meta={"tool_options": get_profile_tool_options(), "show_owner": is_admin, "current_uid": current_user.uid},
    )
    return StandardResponse.success(data=page_data)


@router.post("/set-default")
async def set_default_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    profile = await profile_crud.get(db, profile_id)
    if not profile:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    if profile.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    await validate_profile_for_assignment(db, profile)

    await profile_crud.clear_default_by_uid(db, profile.uid)
    profile.is_default = True
    await db.commit()
    return StandardResponse.success(message=MSG_PROFILE_SET_DEFAULT)


@router.post("/update", response_model=StandardResponse[ProfileResponse])
async def update_profile(
    profile_id: int,
    profile_in: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    if not getattr(current_user, "is_superuser", False) and db_profile.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    if profile_in.configs:
        profile_in.configs = ProfileConfig.model_validate(profile_in.configs).model_dump()
        channel_config = profile_in.configs.get("channel", {})
        await validate_channel_configs(db, channel_config)
        await validate_audit_model_config(db, profile_in.configs.get("security", {}))

    if profile_in.name and profile_in.name != db_profile.name:
        if await profile_crud.get_by_name(db, profile_in.name, uid=db_profile.uid):
            raise ParameterException(ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get_visible(db, profile_in.prompt_id, uid=db_profile.uid):
            raise ResourceNotFoundException(ERR_PROMPT_NOT_FOUND)

    knowledge_base_ids = profile_in.knowledge_base_ids
    db_profile = await profile_crud.update(db, db_obj=db_profile, obj_in=profile_in.model_dump(exclude={"knowledge_base_ids"}, exclude_unset=True))
    await replace_profile_knowledge_base_bindings(db, db_profile.id, db_profile.uid, knowledge_base_ids)
    await db.commit()
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = await build_profile_response(db, db_profile)
    return StandardResponse.success(
        data=res_data,
        message=MSG_PROFILE_UPDATED,
    )


@router.post("/delete")
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    if not getattr(current_user, "is_superuser", False) and db_profile.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    count = len(await profile_crud.get_multi(db, uid=db_profile.uid))
    if count <= 1:
        raise ParameterException(ERR_DELETE_LAST_PROFILE)

    if db_profile.is_default:
        raise ParameterException(ERR_DELETE_DEFAULT_PROFILE)
    has_session_override = await session_crud.has_profile_override(db, profile_id)
    has_platform_assignment = await message_platform_crud.has_profile_assignment(db, profile_id)
    if has_session_override or has_platform_assignment:
        raise ParameterException(ERR_DELETE_BOUND_PROFILE)

    binding_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.profile_id == profile_id))
    for binding in binding_result.scalars().all():
        await db.delete(binding)
    await profile_crud.remove(db, id=profile_id)
    return StandardResponse.success(message=MSG_PROFILE_DELETED)
