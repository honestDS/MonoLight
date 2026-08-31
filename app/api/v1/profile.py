"""Profile API：渠道管理架构适配版

CRUD 支持对话、上下文总结、重排和图像生成渠道；default 校验适配
"""

from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_DELETE_DEFAULT_PROFILE,
    ERR_DELETE_LAST_PROFILE,
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED,
    ERR_PROFILE_MEMORY_CREATE_CONFIRMATION_FORBIDDEN,
    ERR_PROFILE_NAME_EXISTS,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROMPT_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
    MSG_MEMORY_SETTINGS_SUCCESS,
    MSG_PROFILE_CREATED,
    MSG_PROFILE_DELETE_CONFIRMATION_REQUIRED,
    MSG_PROFILE_DELETED,
    MSG_PROFILE_MEMORY_EMBEDDING_CONFIRMED,
    MSG_PROFILE_MEMORY_EMBEDDING_PREVIEW_READY,
    MSG_PROFILE_SET_DEFAULT,
    MSG_PROFILE_UPDATED,
)
from app.core.crud.account.user import user_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.profile.prompt import prompt_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.i18n import t
from app.core.knowledge.bindings import (
    get_user_knowledge_base_ids_for_profile,
    replace_user_knowledge_base_bindings,
)
from app.core.memory import get_memory_settings, update_organization_settings
from app.core.memory.embedding_config import (
    build_memory_runtime,
    confirm_embedding_selection,
    normalize_profile_memory_for_create,
    normalize_profile_memory_for_update,
    preview_embedding_selection,
)
from app.core.profile_deletion import build_profile_deletion_impact, execute_profile_deletion
from app.core.profile_validation import (
    lock_profile_channel_references,
    validate_audit_model_config,
    validate_channel_configs,
    validate_profile_for_assignment,
)
from app.core.security import get_current_user
from app.models.profile import (
    LongTermMemoryOrganizationConfig,
    ProfileConfig,
    ProfileCreate,
    ProfileMemoryEmbeddingConfirmRequest,
    ProfileMemoryEmbeddingPreviewRequest,
    ProfileResponse,
    ProfileUpdate,
)
from app.providers.database import get_db
from app.schemas.memory import MemorySettingsResponse
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(
    prefix="/profiles",
    tags=["Profile Management"],
    dependencies=[Depends(get_current_user)],
)


_MEMORY_STORE_UNSET = object()


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
    {"value": "read_multimodal_file", "label_key": "TOOL_READ_MULTIMODAL_FILE_LABEL"},
]


def get_profile_tool_options() -> list[dict[str, str]]:
    return [{"value": item["value"], "label": t(item["label_key"], default=item["value"])} for item in PROFILE_TOOL_OPTIONS]


async def build_profile_response(
    db: AsyncSession,
    profile: object,
    *,
    username: str | None = None,
    memory_store=_MEMORY_STORE_UNSET,
) -> ProfileResponse:
    item = ProfileResponse.model_validate(profile)
    if username is not None:
        item.username = username
    if item.id is not None:
        item.knowledge_base_ids = await get_user_knowledge_base_ids_for_profile(
            db,
            uid=item.uid,
            profile_id=item.id,
        )
    if memory_store is _MEMORY_STORE_UNSET:
        store = await memory_store_crud.get_snapshot_by_uid(db, uid=item.uid) if item.uid else None
    else:
        store = memory_store
    configs = ProfileConfig.model_validate(item.configs or {}).model_dump()
    configs["memory"]["embedding_channel_id"] = store.active_embedding_channel_id if store and store.active_embedding_revision > 0 else None
    configs["memory"]["embedding_model_id"] = store.active_embedding_model_id if store and store.active_embedding_revision > 0 else None
    item.configs = configs
    item.memory_organization = LongTermMemoryOrganizationConfig(
        auto_organize_enabled=store.auto_organize_enabled if store is not None else False,
        organization_channel_id=store.organization_channel_id if store is not None else None,
        organization_model_id=store.organization_model_id if store is not None else None,
    )
    item.memory_runtime = build_memory_runtime(profile, store)
    return item


async def sync_profile_memory_organization(
    db: AsyncSession,
    *,
    uid: str | None,
    memory_organization: LongTermMemoryOrganizationConfig | None,
) -> None:
    if memory_organization is None or uid is None:
        return
    store = await memory_store_crud.get_by_uid(db, uid=uid)
    if store is None and not memory_organization.auto_organize_enabled and memory_organization.organization_channel_id is None and memory_organization.organization_model_id is None:
        return
    await update_organization_settings(
        db,
        uid=uid,
        auto_organize_enabled=memory_organization.auto_organize_enabled,
        organization_channel_id=memory_organization.organization_channel_id,
        organization_model_id=memory_organization.organization_model_id,
        commit=False,
    )


async def get_memory_embedding_target_uid(db: AsyncSession, profile_id: int, current_user) -> str:
    profile = await profile_crud.get(db, profile_id)
    if profile is None or (not getattr(current_user, "is_superuser", False) and profile.uid != current_user.uid):
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    if profile.uid is None:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    return profile.uid


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
    if profile_in.confirm_memory_embedding_selection or profile_in.memory_embedding_selection_signature is not None:
        raise ParameterException(ERR_PROFILE_MEMORY_CREATE_CONFIRMATION_FORBIDDEN)
    profile_in.configs = normalize_profile_memory_for_create(
        profile_in.configs,
        await memory_store_crud.get_snapshot_by_uid(db, uid=profile_in.uid),
    )
    try:
        locked_channels = await lock_profile_channel_references(
            db,
            configs=profile_in.configs,
            memory_organization=profile_in.memory_organization,
        )
        await validate_channel_configs(
            db,
            profile_in.configs.get("channel", {}),
            locked_channels=locked_channels,
        )
        await validate_audit_model_config(
            db,
            profile_in.configs.get("security", {}),
            locked_channels=locked_channels,
        )

        if await profile_crud.get_by_name(db, profile_in.name, uid=profile_in.uid):
            raise ParameterException(ERR_PROFILE_NAME_EXISTS)

        if profile_in.prompt_id:
            if not await prompt_crud.get_visible(db, profile_in.prompt_id, uid=profile_in.uid):
                raise ParameterException(ERR_PROMPT_NOT_FOUND)

        knowledge_base_ids = profile_in.knowledge_base_ids
        memory_organization = profile_in.memory_organization
        db_profile = await profile_crud.create(
            db,
            obj_in=profile_in.model_dump(
                exclude={
                    "knowledge_base_ids",
                    "confirm_memory_embedding_selection",
                    "memory_embedding_selection_signature",
                    "memory_organization",
                }
            ),
            commit=False,
        )
        await replace_user_knowledge_base_bindings(
            db,
            uid=db_profile.uid,
            profile_id=db_profile.id,
            knowledge_base_ids=knowledge_base_ids,
        )
        await sync_profile_memory_organization(db, uid=db_profile.uid, memory_organization=memory_organization)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
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

    memory_stores = await memory_store_crud.get_multi_by_uids(db, uids={profile.uid for profile in profiles if profile.uid})
    results = []
    for p in profiles:
        item = await build_profile_response(
            db,
            p,
            username=user_map.get(p.uid) if is_admin else None,
            memory_store=memory_stores.get(p.uid),
        )
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
    profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile_id,
        uid=current_user.uid,
    )
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
    if profile_in.confirm_memory_embedding_selection or profile_in.memory_embedding_selection_signature is not None:
        raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)

    if profile_in.configs is not None:
        profile_in.configs = ProfileConfig.model_validate(profile_in.configs).model_dump()
        profile_in.configs = normalize_profile_memory_for_update(
            db_profile,
            profile_in.configs,
            await memory_store_crud.get_by_uid(db, uid=db_profile.uid),
        )
    else:
        profile_in.configs = normalize_profile_memory_for_update(
            db_profile,
            db_profile.configs or {},
            await memory_store_crud.get_snapshot_by_uid(db, uid=db_profile.uid),
        )
    try:
        old_channel_ids = []
        old_cfg = ProfileConfig.model_validate(db_profile.configs or {})
        for old_channel_config in (
            old_cfg.channel.chat_channel,
            old_cfg.channel.context_summary_channel,
            old_cfg.channel.rerank_channel,
            old_cfg.channel.image_generation_channel,
        ):
            for old_rule in old_channel_config.rules:
                if old_rule.channel_id:
                    old_channel_ids.append(old_rule.channel_id)
        if old_cfg.security.audit_channel_id:
            old_channel_ids.append(old_cfg.security.audit_channel_id)
        if old_cfg.memory.embedding_channel_id:
            old_channel_ids.append(old_cfg.memory.embedding_channel_id)

        locked_channels = await lock_profile_channel_references(
            db,
            configs=profile_in.configs,
            memory_organization=profile_in.memory_organization,
            extra_channel_ids=old_channel_ids,
        )
        db_profile = await profile_crud.lock_for_runtime_use(
            db,
            profile_id=profile_id,
            uid=db_profile.uid,
        )
        if not db_profile:
            raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
        if not getattr(current_user, "is_superuser", False) and db_profile.uid != current_user.uid:
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
        await validate_channel_configs(
            db,
            profile_in.configs.get("channel", {}),
            locked_channels=locked_channels,
        )
        await validate_audit_model_config(
            db,
            profile_in.configs.get("security", {}),
            locked_channels=locked_channels,
        )

        if profile_in.name and profile_in.name != db_profile.name:
            if await profile_crud.get_by_name(db, profile_in.name, uid=db_profile.uid):
                raise ParameterException(ERR_PROFILE_NAME_EXISTS)

        if profile_in.prompt_id:
            if not await prompt_crud.get_visible(db, profile_in.prompt_id, uid=db_profile.uid):
                raise ResourceNotFoundException(ERR_PROMPT_NOT_FOUND)

        knowledge_base_ids = profile_in.knowledge_base_ids
        memory_organization = profile_in.memory_organization
        db_profile = await profile_crud.update(
            db,
            db_obj=db_profile,
            obj_in=profile_in.model_dump(
                exclude={
                    "knowledge_base_ids",
                    "confirm_memory_embedding_selection",
                    "memory_embedding_selection_signature",
                    "memory_organization",
                },
                exclude_unset=True,
            ),
            commit=False,
        )
        await replace_user_knowledge_base_bindings(
            db,
            uid=db_profile.uid,
            profile_id=db_profile.id,
            knowledge_base_ids=knowledge_base_ids,
        )
        await sync_profile_memory_organization(db, uid=db_profile.uid, memory_organization=memory_organization)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = await build_profile_response(db, db_profile)
    return StandardResponse.success(
        data=res_data,
        message=MSG_PROFILE_UPDATED,
    )


@router.get("/memory-settings", response_model=StandardResponse[MemorySettingsResponse])
async def get_profile_memory_settings(
    uid: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if uid is not None and not getattr(current_user, "is_superuser", False) and uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
    result = await get_memory_settings(db, uid=uid or current_user.uid)
    return StandardResponse.success(data=result, message=MSG_MEMORY_SETTINGS_SUCCESS)


@router.post("/memory-embedding-preview")
async def profile_memory_embedding_preview(
    request: ProfileMemoryEmbeddingPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        target_uid = await get_memory_embedding_target_uid(db, request.profile_id, current_user)
        data = await preview_embedding_selection(
            db,
            uid=target_uid,
            profile_id=request.profile_id,
            embedding_channel_id=request.embedding_channel_id,
            embedding_model_id=request.embedding_model_id,
        )
    except Exception:
        await db.rollback()
        raise
    return StandardResponse.success(data=data, message=MSG_PROFILE_MEMORY_EMBEDDING_PREVIEW_READY)


@router.post("/memory-embedding-confirm", response_model=StandardResponse[ProfileResponse])
async def profile_memory_embedding_confirm(
    request: ProfileMemoryEmbeddingConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        target_uid = await get_memory_embedding_target_uid(db, request.profile_id, current_user)
        profile, _store = await confirm_embedding_selection(
            db,
            uid=target_uid,
            profile_id=request.profile_id,
            memory=request.memory,
            embedding_selection_signature=request.embedding_selection_signature,
        )
    except Exception:
        await db.rollback()
        raise
    refreshed_profile = await profile_crud.get_with_relations(db, profile.id)
    data = await build_profile_response(db, refreshed_profile)
    return StandardResponse.success(data=data, message=MSG_PROFILE_MEMORY_EMBEDDING_CONFIRMED)


@router.post("/delete")
async def delete_profile(
    profile_id: int,
    confirm_impact: bool = False,
    impact_token: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    if not getattr(current_user, "is_superuser", False) and db_profile.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    if await profile_crud.count_by_uid(db, uid=db_profile.uid) <= 1:
        raise ParameterException(ERR_DELETE_LAST_PROFILE)
    if db_profile.is_default:
        raise ParameterException(ERR_DELETE_DEFAULT_PROFILE)

    impact = await build_profile_deletion_impact(db, profile=db_profile)
    if not confirm_impact or impact_token != impact["impact_token"]:
        return StandardResponse.success(
            data={"requires_confirmation": True, **impact},
            message=MSG_PROFILE_DELETE_CONFIRMATION_REQUIRED,
        )

    try:
        locked_profile = await profile_crud.lock_for_runtime_use(
            db,
            profile_id=profile_id,
            uid=db_profile.uid,
        )
        if locked_profile is None:
            raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
        if locked_profile.is_default:
            raise ParameterException(ERR_DELETE_DEFAULT_PROFILE)
        if await profile_crud.count_by_uid(db, uid=locked_profile.uid) <= 1:
            raise ParameterException(ERR_DELETE_LAST_PROFILE)

        current_impact = await build_profile_deletion_impact(db, profile=locked_profile)
        if impact_token != current_impact["impact_token"]:
            await db.rollback()
            return StandardResponse.success(
                data={"requires_confirmation": True, **current_impact},
                message=MSG_PROFILE_DELETE_CONFIRMATION_REQUIRED,
            )

        await execute_profile_deletion(
            db,
            profile=locked_profile,
            impact=current_impact,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return StandardResponse.success(
        data={"requires_confirmation": False},
        message=MSG_PROFILE_DELETED,
    )
