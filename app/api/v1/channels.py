"""Channel API：渠道管理架构适配版

CRUD 支持 model_ids 字段；移除 usage 字段
"""

import copy
import json
import re

from fastapi import (
    APIRouter,
    Body,
    Depends,
)
from pydantic import (
    BaseModel,
)
from pydantic import (
    Field as PydanticField,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.core import constants
from app.core.crud.channel import channel_crud
from app.core.crud.profile import profile_crud
from app.core.exceptions import (
    BaseBusinessException,
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.models.channel import (
    ChannelCreate,
    ChannelListResponse,
    ChannelModelItem,
    ChannelResponse,
    ChannelType,
    ChannelUpdate,
    ImageGenerationQuality,
    ImageGenerationSize,
    ModelUsage,
    validate_channel_model_ids,
)
from app.models.message import InternalMessage, MessageRole
from app.providers.database import get_db
from app.providers.embedding import EmbeddingClient
from app.providers.image_generation import ImageGenerationClient
from app.providers.llm.client import LLMClient
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(prefix="/channels", tags=["Channels"], dependencies=[Depends(get_current_user)])

# 渠道用途映射：统一定义，避免重复
CHANNEL_USAGE_MAP = {
    "chat_channel": ModelUsage.CHAT.value,
    "embedding_channel": ModelUsage.EMBEDDING.value,
    "rerank_channel": ModelUsage.RERANK.value,
    "image_generation_channel": ModelUsage.IMAGE_GENERATION.value,
}


class ChannelModelListRequest(BaseModel):
    channel_type: ChannelType | None = None
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = PydanticField(30.0, gt=0, le=120)


class ChannelChatTestRequest(BaseModel):
    channel_type: ChannelType | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    temperature: float | None = PydanticField(None, ge=0, le=2.0)
    top_p: float | None = PydanticField(None, ge=0, le=1.0)
    max_tokens: int | None = PydanticField(None, ge=0)
    timeout: float = PydanticField(60.0, gt=0, le=600)


class ChannelImageGenerationTestRequest(BaseModel):
    channel_type: ChannelType | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    size: ImageGenerationSize = ImageGenerationSize.SIZE_1024X1024
    quality: ImageGenerationQuality | None = ImageGenerationQuality.AUTO
    timeout: float = PydanticField(60.0, gt=0, le=600)


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


def _is_same_channel(rule: dict, channel_id: int) -> bool:
    return str(rule.get("channel_id")) == str(channel_id)


def _normalize_channel_model_ids(model_ids: list[dict] | None) -> list[dict]:
    normalized_model_ids = []
    for item in model_ids or []:
        normalized_item = copy.deepcopy(item)
        if str(normalized_item.get("usage")) != ModelUsage.IMAGE_GENERATION.value:
            normalized_item.pop("size", None)
            normalized_item.pop("quality", None)
        normalized_model_ids.append(normalized_item)
    return normalized_model_ids


def _get_existing_model_ids_by_usage(model_ids: list[dict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in result:
            result[item_usage].add(str(item_model_id))
    return result


def _clean_channel_rules_from_configs(
    configs: dict,
    channel_id: int,
    model_ids: list[dict],
) -> int:
    existing_model_ids_by_usage = _get_existing_model_ids_by_usage(model_ids)
    channel_config = configs.get("channel") or {}
    removed_count = 0
    profile_changed = False

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue

        existing_model_ids = existing_model_ids_by_usage.get(usage, set())
        old_rules = channel["rules"]
        new_rules = []
        rules_changed = False
        for rule in old_rules:
            if _is_same_channel(rule, channel_id) and str(rule.get("model_id")) not in existing_model_ids:
                removed_count += 1
                rules_changed = True
                continue

            new_rules.append(rule)

        if rules_changed:
            channel["rules"] = new_rules
            profile_changed = True

    return removed_count if profile_changed else 0


async def _remove_unavailable_channel_rules(
    db: AsyncSession,
    channel_id: int,
    model_ids: list[dict],
) -> int:
    """批量清理Profile中失效的渠道规则。

    采用游标分页避免 offset 窗口错位，调用方负责统一提交事务。
    """
    batch_size = 100
    last_id = 0
    total_removed = 0

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = profile.configs or {}
            profile_removed_count = _clean_channel_rules_from_configs(configs, channel_id, model_ids)
            if profile_removed_count > 0:
                profile.configs = configs
                flag_modified(profile, "configs")
                db.add(profile)
                batch_changed = True
                total_removed += profile_removed_count

        if batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_removed


def _model_entry_signature(item: dict) -> str:
    normalized = ChannelModelItem.model_validate(item).model_dump(exclude={"model_id"})
    return json.dumps(normalized, sort_keys=True, default=str)


def _collect_channel_rule_model_ids(configs: dict, channel_id: int) -> dict[str, set[str]]:
    channel_config = configs.get("channel") or {}
    referenced_model_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_channel(rule, channel_id) and rule.get("model_id"):
                referenced_model_ids_by_usage[usage].add(str(rule["model_id"]))

    return referenced_model_ids_by_usage


def _build_model_id_rename_index(old_model_ids: list[dict], new_model_ids: list[dict]) -> dict[str, dict]:
    old_by_usage_and_id: dict[tuple[str, str], dict] = {}
    for item in old_model_ids:
        item_usage = item.get("usage")
        item_model_id = item.get("model_id")
        if item_usage and item_model_id:
            old_by_usage_and_id[(str(item_usage), str(item_model_id))] = item

    old_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in old_model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in old_ids_by_usage:
            old_ids_by_usage[item_usage].add(str(item_model_id))

    new_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in new_model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in new_ids_by_usage:
            new_ids_by_usage[item_usage].add(str(item_model_id))

    new_by_usage_and_signature: dict[tuple[str, str], list[dict]] = {}
    for item in new_model_ids:
        usage = str(item.get("usage"))
        model_id = item.get("model_id")
        if usage not in new_ids_by_usage or not model_id:
            continue
        signature = _model_entry_signature(item)
        new_by_usage_and_signature.setdefault((usage, signature), []).append(item)

    return {
        "old_by_usage_and_id": old_by_usage_and_id,
        "old_ids_by_usage": old_ids_by_usage,
        "new_ids_by_usage": new_ids_by_usage,
        "new_by_usage_and_signature": new_by_usage_and_signature,
    }


def _compute_model_id_renames(
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    referenced_model_ids: dict[str, set[str]] | None = None,
    rename_index: dict[str, dict] | None = None,
) -> dict[str, dict[str, str]]:
    # 仅对配置实际引用且已消失的旧模型 ID，按非 model_id 配置精确匹配唯一新模型。
    index = rename_index or _build_model_id_rename_index(old_model_ids, new_model_ids)
    old_by_usage_and_id = index["old_by_usage_and_id"]
    old_ids_by_usage = index["old_ids_by_usage"]
    new_ids_by_usage = index["new_ids_by_usage"]
    new_by_usage_and_signature = index["new_by_usage_and_signature"]

    referenced_ids_by_usage = referenced_model_ids or old_ids_by_usage
    renames: dict[str, dict[str, str]] = {}

    for usage, model_ids in referenced_ids_by_usage.items():
        if usage not in old_ids_by_usage:
            continue
        for old_model_id in model_ids:
            if old_model_id in new_ids_by_usage[usage]:
                continue

            old_item = old_by_usage_and_id.get((usage, old_model_id))
            if not old_item:
                continue

            signature = _model_entry_signature(old_item)
            candidates = []
            for item in new_by_usage_and_signature.get((usage, signature), []):
                new_model_id = str(item.get("model_id"))
                if new_model_id not in old_ids_by_usage[usage]:
                    candidates.append(item)
            if len(candidates) != 1:
                continue

            renames.setdefault(usage, {})[old_model_id] = str(candidates[0]["model_id"])

    return renames


def _apply_model_id_renames_to_configs(
    configs: dict,
    channel_id: int,
    renames: dict[str, dict[str, str]],
) -> int:
    channel_config = configs.get("channel") or {}
    updated_count = 0

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        rename_map = renames.get(usage)
        if not rename_map:
            continue
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_channel(rule, channel_id):
                old_model_id = str(rule.get("model_id"))
                if old_model_id in rename_map:
                    rule["model_id"] = rename_map[old_model_id]
                    updated_count += 1

    return updated_count


async def _sync_channel_model_id_renames(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
) -> int:
    """批量同步Profile中的模型ID重命名。

    采用游标分页避免 offset 窗口错位，调用方负责统一提交事务。
    """
    batch_size = 100
    last_id = 0
    total_updated = 0
    rename_index = _build_model_id_rename_index(old_model_ids, new_model_ids)

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = profile.configs or {}
            referenced_model_ids = _collect_channel_rule_model_ids(configs, channel_id)
            renames = _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids, rename_index)
            if not renames:
                continue

            profile_updated_count = _apply_model_id_renames_to_configs(configs, channel_id, renames)
            if profile_updated_count > 0:
                profile.configs = configs
                flag_modified(profile, "configs")
                db.add(profile)
                batch_changed = True
                total_updated += profile_updated_count

        if batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_updated


def _get_chat_model_ids(model_ids: list[dict]) -> set[str]:
    result: set[str] = set()
    for item in model_ids:
        if str(item.get("usage")) == ModelUsage.CHAT.value and item.get("model_id"):
            result.add(str(item.get("model_id")))
    return result


async def _sync_audit_model_id_renames(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
) -> int:
    """同步引用该渠道的 Profile 审计模型重命名，调用方负责统一提交事务。"""
    batch_size = 100
    last_id = 0
    total_updated = 0
    rename_index = _build_model_id_rename_index(old_model_ids, new_model_ids)

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = profile.configs or {}
            security_config = configs.get("security") if isinstance(configs.get("security"), dict) else {}
            audit_model_id = security_config.get("audit_model_id")
            if str(security_config.get("audit_channel_id")) != str(channel_id) or not audit_model_id:
                continue

            renames = _compute_model_id_renames(
                old_model_ids,
                new_model_ids,
                {ModelUsage.CHAT.value: {str(audit_model_id)}},
                rename_index,
            )
            new_model_id = renames.get(ModelUsage.CHAT.value, {}).get(str(audit_model_id))
            if not new_model_id:
                continue

            security_config["audit_model_id"] = new_model_id
            configs["security"] = security_config
            profile.configs = configs
            flag_modified(profile, "configs")
            db.add(profile)
            batch_changed = True
            total_updated += 1

        if batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_updated


async def _clear_unavailable_audit_model_refs(
    db: AsyncSession,
    channel_id: int,
    model_ids: list[dict],
) -> int:
    """清理引用已不可用审计模型的 Profile 安全配置，调用方负责统一提交事务。"""
    available_chat_model_ids = _get_chat_model_ids(model_ids)
    batch_size = 100
    last_id = 0
    total_cleared = 0

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = profile.configs or {}
            security_config = configs.get("security") if isinstance(configs.get("security"), dict) else {}
            audit_channel_id = security_config.get("audit_channel_id")
            audit_model_id = security_config.get("audit_model_id")
            if str(audit_channel_id) != str(channel_id):
                continue

            if audit_model_id and str(audit_model_id) in available_chat_model_ids:
                continue

            if audit_channel_id is None and audit_model_id is None:
                continue

            security_config["audit_channel_id"] = None
            security_config["audit_model_id"] = None
            configs["security"] = security_config
            profile.configs = configs
            flag_modified(profile, "configs")
            db.add(profile)
            batch_changed = True
            total_cleared += 1

        if batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_cleared


@router.post("/create", response_model=StandardResponse)
async def create_channel(
    channel_in: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    channel_in.model_ids = _normalize_channel_model_ids(channel_in.model_ids)
    validation_error, validation_kwargs = validate_channel_model_ids(channel_in.model_ids)
    if validation_error:
        return StandardResponse.error(code=422, message=validation_error, **validation_kwargs)

    if channel_in.base_url and not re.match(r"^https?://", channel_in.base_url):
        return StandardResponse.error(code=422, message=constants.ERR_CHANNEL_BASE_URL_SCHEME)

    if channel_in.model_ids and not channel_in.base_url:
        return StandardResponse.error(code=422, message=constants.ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)

    if await channel_crud.get_by_name(db, channel_in.name):
        raise ParameterException(constants.ERR_CHANNEL_NAME_EXISTS)

    db_obj = await channel_crud.create_with_plain_api_key(db, obj_in=channel_in)

    return StandardResponse.success(
        data=ChannelResponse.model_validate(db_obj),
        message=constants.MSG_CHANNEL_CREATED,
    )


@router.get("/types", response_model=StandardResponse)
async def get_channel_types():
    return StandardResponse.success(
        data={
            "channel_types": [e.value for e in ChannelType],
            "model_usages": [e.value for e in ModelUsage],
        }
    )


@router.get("/list", response_model=StandardResponse)
async def list_channels(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    skip = (page - 1) * size
    channels = await channel_crud.get_multi(db, skip=skip, limit=size)
    total = await channel_crud.count(db)

    page_data = PageData(
        items=[ChannelListResponse.model_validate(item) for item in channels],
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(data=page_data)


@router.get("/get", response_model=StandardResponse)
async def get_channel(channel_id: int, db: AsyncSession = Depends(get_db)):
    db_obj = await channel_crud.get(db, channel_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_CHANNEL_NOT_FOUND)
    return StandardResponse.success(data=ChannelResponse.model_validate(db_obj))


@router.post("/models", response_model=StandardResponse)
async def list_channel_models(
    payload: ChannelModelListRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    """按渠道类型检测模型列表。"""
    channel_type = payload.channel_type
    api_key = payload.api_key
    base_url = payload.base_url

    if not channel_type:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_CHANNEL_TYPE)
    if not base_url:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_API_KEY)

    try:
        models = await LLMClient.list_models(
            protocol=channel_type.value if isinstance(channel_type, ChannelType) else str(channel_type),
            api_key=api_key,
            base_url=base_url,
            timeout=payload.timeout,
        )
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_FAILED, detail=str(e)) from e

    return StandardResponse.success(
        data={"models": models},
        message=constants.MSG_CHANNEL_MODEL_LIST_SUCCESS,
    )


@router.post("/test-chat", response_model=StandardResponse)
async def test_channel_chat(
    payload: ChannelChatTestRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    channel_type = payload.channel_type
    api_key = payload.api_key
    base_url = payload.base_url
    model_id = payload.model_id

    if not channel_type:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_CHANNEL_TYPE)
    if not base_url:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_API_KEY)
    if not model_id or not model_id.strip():
        raise ParameterException(constants.ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID)

    try:
        response = await LLMClient.generate(
            protocol=channel_type.value if isinstance(channel_type, ChannelType) else str(channel_type),
            api_key=api_key,
            base_url=base_url,
            model_id=model_id.strip(),
            messages=[InternalMessage(role=MessageRole.USER, content="你好")],
            temperature=payload.temperature if payload.temperature is not None else 0.7,
            max_tokens=payload.max_tokens or 0,
            top_p=payload.top_p,
            timeout=payload.timeout,
        )
        content = response.message.content
        if isinstance(content, str):
            reply = content.strip()
        else:
            reply = json.dumps(content, ensure_ascii=False) if content else ""
        if not reply:
            raise ParameterException(constants.ERR_CHANNEL_CHAT_TEST_EMPTY_RESPONSE)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e

    return StandardResponse.success(
        data={"model": response.model, "reply": reply, "usage": response.usage},
        message=constants.MSG_CHANNEL_CHAT_TEST_SUCCESS,
    )


@router.post("/test-image-generation", response_model=StandardResponse)
async def test_channel_image_generation(
    payload: ChannelImageGenerationTestRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    channel_type = payload.channel_type
    api_key = payload.api_key
    base_url = payload.base_url
    model_id = payload.model_id

    if not channel_type:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_CHANNEL_TYPE)
    if not base_url:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(constants.ERR_CHANNEL_MODEL_LIST_NO_API_KEY)
    if not model_id or not model_id.strip():
        raise ParameterException(constants.ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID)

    try:
        response = await ImageGenerationClient.generate_image(
            channel_type=channel_type,
            api_key=api_key,
            base_url=base_url,
            model_id=model_id.strip(),
            prompt="A simple red apple on a white background.",
            size=payload.size,
            n=1,
            quality=payload.quality,
            timeout=payload.timeout,
        )
        images = response.get("data") if isinstance(response, dict) else None
        if not isinstance(images, list) or not images:
            raise ParameterException(constants.ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE)
        first_image = images[0] if isinstance(images[0], dict) else {}
        if not first_image.get("url") and not first_image.get("b64_json"):
            raise ParameterException(constants.ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e

    return StandardResponse.success(
        data={
            "model": response.get("model", model_id.strip()) if isinstance(response, dict) else model_id.strip(),
            "image": first_image,
        },
        message=constants.MSG_CHANNEL_IMAGE_GENERATION_TEST_SUCCESS,
    )


@router.post("/update", response_model=StandardResponse)
async def update_channel(
    channel_id: int,
    channel_in: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_obj = await channel_crud.get(db, channel_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_CHANNEL_NOT_FOUND)

    if channel_in.name and channel_in.name != db_obj.name:
        if await channel_crud.get_by_name(db, channel_in.name):
            raise ParameterException(constants.ERR_CHANNEL_NAME_EXISTS)

    # 校验 model_ids 合法性（如果传入）
    if channel_in.model_ids is not None:
        channel_in.model_ids = _normalize_channel_model_ids(channel_in.model_ids)
        validation_error, validation_kwargs = validate_channel_model_ids(channel_in.model_ids)
        if validation_error:
            return StandardResponse.error(code=422, message=validation_error, **validation_kwargs)

    if channel_in.base_url and not re.match(r"^https?://", channel_in.base_url):
        return StandardResponse.error(code=422, message=constants.ERR_CHANNEL_BASE_URL_SCHEME)

    # 跨字段校验：所有可调用模型类型都依赖 base_url 拼接供应商接口路径。
    final_model_ids = channel_in.model_ids if channel_in.model_ids is not None else db_obj.model_ids
    final_base_url = channel_in.base_url if "base_url" in channel_in.model_fields_set else db_obj.base_url
    if final_model_ids and not final_base_url:
        return StandardResponse.error(code=422, message=constants.ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)

    # 更新前捕获旧 model_ids，用于推断 model_id 重命名并同步到绑定的 profile
    old_model_ids = copy.deepcopy(db_obj.model_ids) if db_obj.model_ids else []

    update_data = channel_in.model_dump(exclude_unset=True)
    synced_profile_rules = 0
    removed_profile_rules = 0
    synced_audit_refs = 0
    cleared_audit_refs = 0

    try:
        api_key = update_data.pop("api_key", None)
        for field, value in update_data.items():
            setattr(db_obj, field, value)
        if api_key is not None:
            db_obj.set_api_key_plaintext(api_key)

        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)

        if channel_in.model_ids is not None:
            # 先把绑定该渠道的 profile 渠道规则与审计模型引用中被重命名的 model_id 同步更新，
            # 再清理失效引用，使重命名后的配置得以保留而非被当作删除清除
            synced_profile_rules = await _sync_channel_model_id_renames(db, channel_id, old_model_ids, db_obj.model_ids or [])
            synced_audit_refs = await _sync_audit_model_id_renames(db, channel_id, old_model_ids, db_obj.model_ids or [])
            removed_profile_rules = await _remove_unavailable_channel_rules(db, channel_id, db_obj.model_ids)
            cleared_audit_refs = await _clear_unavailable_audit_model_refs(db, channel_id, db_obj.model_ids or [])

        await db.commit()
        await db.refresh(db_obj)
    except Exception:
        await db.rollback()
        raise

    return StandardResponse.success(
        data={
            "channel": ChannelResponse.model_validate(db_obj),
            "removed_profile_rules": removed_profile_rules,
            "synced_profile_rules": synced_profile_rules,
            "synced_audit_refs": synced_audit_refs,
            "cleared_audit_refs": cleared_audit_refs,
        },
        message=constants.MSG_CHANNEL_UPDATED,
    )


@router.post("/delete")
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_obj = await channel_crud.get(db, channel_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_CHANNEL_NOT_FOUND)

    try:
        removed_profile_rules = await _remove_unavailable_channel_rules(db, channel_id, [])
        cleared_audit_refs = await _clear_unavailable_audit_model_refs(db, channel_id, [])
        await db.delete(db_obj)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return StandardResponse.success(
        data={"removed_profile_rules": removed_profile_rules, "cleared_audit_refs": cleared_audit_refs},
        message=constants.MSG_CHANNEL_DELETED,
    )


@router.post("/test-embedding-dimension")
async def test_embedding_dimension(
    channel_id: int,
    model_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    """自动检测嵌入模型的输出维度。"""
    db_obj = await channel_crud.get(db, channel_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_CHANNEL_NOT_FOUND)

    if not db_obj.base_url:
        raise ParameterException(constants.ERR_CHANNEL_TEST_NO_URL)

    try:
        embedding_response = await EmbeddingClient.get_embeddings(
            channel_type=db_obj.channel_type,
            api_key=db_obj.get_decrypted_api_key(),
            base_url=db_obj.base_url,
            model_id=model_id,
            input_texts=["dimension test"],
        )
        if "data" in embedding_response and len(embedding_response["data"]) > 0:
            dimension = len(embedding_response["data"][0]["embedding"])
            return StandardResponse.success(
                data={"dimension": dimension},
                message=constants.MSG_CHANNEL_TEST_SUCCESS,
                dim=dimension,
            )
        else:
            raise ParameterException(constants.ERR_CHANNEL_TEST_DIMENSION_ERROR)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(constants.ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e
