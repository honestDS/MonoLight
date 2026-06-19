"""Provider API：渠道管理架构适配版

CRUD 支持 model_ids 字段；移除 usage 字段
"""

import copy
import json

from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core import constants
from app.core.crud.profile import profile_crud
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
    ProviderModelItem,
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


def _is_same_provider(rule: dict, provider_id: int) -> bool:
    return str(rule.get("provider_id")) == str(provider_id)


def _get_enabled_models_by_usage(model_ids: list[dict]) -> dict[str, set[str]]:
    return {
        usage.value: {
            str(item.get("model_id"))
            for item in model_ids
            if str(item.get("usage")) == usage.value and item.get("model_id")
        }
        for usage in ModelUsage
    }


def _clean_provider_rules_from_configs(
    configs: dict,
    provider_id: int,
    model_ids: list[dict],
) -> int:
    enabled_models_by_usage = _get_enabled_models_by_usage(model_ids)
    channel_usage_map = {
        "chat_channel": ModelUsage.CHAT.value,
        "embedding_channel": ModelUsage.EMBEDDING.value,
        "rerank_channel": ModelUsage.RERANK.value,
    }
    provider_config = configs.get("provider") or {}
    removed_count = 0
    profile_changed = False

    for channel_key, usage in channel_usage_map.items():
        channel = provider_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue

        enabled_model_ids = enabled_models_by_usage.get(usage, set())
        old_rules = channel["rules"]
        new_rules = []
        rules_changed = False
        for rule in old_rules:
            if _is_same_provider(rule, provider_id) and str(rule.get("model_id")) not in enabled_model_ids:
                removed_count += 1
                rules_changed = True
                continue

            new_rules.append(rule)

        if rules_changed:
            channel["rules"] = new_rules
            profile_changed = True

    return removed_count if profile_changed else 0


async def _remove_unavailable_provider_rules(
    db: AsyncSession,
    provider_id: int,
    model_ids: list[dict],
) -> int:
    profiles = await profile_crud.get_multi(db, limit=10000)
    changed = False
    removed_count = 0

    for profile in profiles:
        configs = profile.configs or {}
        profile_removed_count = _clean_provider_rules_from_configs(configs, provider_id, model_ids)
        if profile_removed_count > 0:
            profile.configs = configs
            flag_modified(profile, "configs")
            db.add(profile)
            changed = True
            removed_count += profile_removed_count

    if changed:
        await db.commit()

    return removed_count


def _model_entry_signature(item: dict) -> str:
    normalized = ProviderModelItem.model_validate(item).model_dump(exclude={"model_id"})
    return json.dumps(normalized, sort_keys=True, default=str)


def _collect_provider_rule_model_ids(configs: dict, provider_id: int) -> dict[str, set[str]]:
    channel_usage_map = {
        "chat_channel": ModelUsage.CHAT.value,
        "embedding_channel": ModelUsage.EMBEDDING.value,
        "rerank_channel": ModelUsage.RERANK.value,
    }
    provider_config = configs.get("provider") or {}
    refs: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}

    for channel_key, usage in channel_usage_map.items():
        channel = provider_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_provider(rule, provider_id) and rule.get("model_id"):
                refs[usage].add(str(rule["model_id"]))

    return refs


def _compute_model_id_renames(
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    referenced_model_ids: dict[str, set[str]] | None = None,
) -> dict[str, dict[str, str]]:
    # 基于“配置文件实际引用的旧模型 ID”推断重命名，而不是基于位置或数量。
    # 对每个被 Profile 渠道规则引用的旧 model_id：
    # - 若旧 model_id 仍存在于新 provider 的同用途模型列表中，则无需同步；
    # - 若旧 model_id 已消失，则用旧模型条目的非 model_id 配置与新模型条目做精确匹配；
    # - 仅当匹配到唯一的新 model_id 时，才同步 Profile 引用，避免同配置多候选时误配对。
    old_by_usage_and_id = {
        (str(item.get("usage")), str(item.get("model_id"))): item
        for item in old_model_ids
        if item.get("usage") and item.get("model_id")
    }
    old_ids_by_usage = {
        usage.value: {str(item.get("model_id")) for item in old_model_ids if str(item.get("usage")) == usage.value and item.get("model_id")}
        for usage in ModelUsage
    }
    new_ids_by_usage = {
        usage.value: {str(item.get("model_id")) for item in new_model_ids if str(item.get("usage")) == usage.value and item.get("model_id")}
        for usage in ModelUsage
    }
    new_by_usage_and_signature: dict[tuple[str, str], list[dict]] = {}
    for item in new_model_ids:
        usage = str(item.get("usage"))
        model_id = item.get("model_id")
        if usage not in new_ids_by_usage or not model_id:
            continue
        signature = _model_entry_signature(item)
        new_by_usage_and_signature.setdefault((usage, signature), []).append(item)

    refs = referenced_model_ids or old_ids_by_usage
    renames: dict[str, dict[str, str]] = {}

    for usage, model_ids in refs.items():
        if usage not in old_ids_by_usage:
            continue
        for old_mid in model_ids:
            if old_mid in new_ids_by_usage[usage]:
                continue

            old_item = old_by_usage_and_id.get((usage, old_mid))
            if not old_item:
                continue

            signature = _model_entry_signature(old_item)
            candidates = [
                item
                for item in new_by_usage_and_signature.get((usage, signature), [])
                if str(item.get("model_id")) not in old_ids_by_usage[usage]
            ]
            if len(candidates) != 1:
                continue

            renames.setdefault(usage, {})[old_mid] = str(candidates[0]["model_id"])

    return renames


def _apply_model_id_renames_to_configs(
    configs: dict,
    provider_id: int,
    renames: dict[str, dict[str, str]],
) -> int:
    channel_usage_map = {
        "chat_channel": ModelUsage.CHAT.value,
        "embedding_channel": ModelUsage.EMBEDDING.value,
        "rerank_channel": ModelUsage.RERANK.value,
    }
    provider_config = configs.get("provider") or {}
    updated_count = 0

    for channel_key, usage in channel_usage_map.items():
        rename_map = renames.get(usage)
        if not rename_map:
            continue
        channel = provider_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_provider(rule, provider_id):
                old_mid = str(rule.get("model_id"))
                if old_mid in rename_map:
                    rule["model_id"] = rename_map[old_mid]
                    updated_count += 1

    return updated_count


async def _sync_provider_model_id_renames(
    db: AsyncSession,
    provider_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
) -> int:
    profiles = await profile_crud.get_multi(db, limit=10000)
    changed = False
    updated_count = 0

    for profile in profiles:
        configs = profile.configs or {}
        referenced_model_ids = _collect_provider_rule_model_ids(configs, provider_id)
        renames = _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids)
        if not renames:
            continue

        profile_updated_count = _apply_model_id_renames_to_configs(configs, provider_id, renames)
        if profile_updated_count > 0:
            profile.configs = configs
            flag_modified(profile, "configs")
            db.add(profile)
            changed = True
            updated_count += profile_updated_count

    if changed:
        await db.commit()

    return updated_count


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

    # 校验 model_ids 合法性（如果传入）
    if provider_in.model_ids is not None:
        seen_model_keys: set[tuple[str, str]] = set()
        for i, item in enumerate(provider_in.model_ids):
            try:
                validated = ProviderModelItem.model_validate(item)
            except Exception as e:
                raise ParameterException(f"model_ids[{i}] 校验失败: {e}")

            model_key = (validated.usage.value, validated.model_id)
            if model_key in seen_model_keys:
                raise ParameterException(f"同一用途下模型 ID 不能重复: {validated.usage.value}/{validated.model_id}")
            seen_model_keys.add(model_key)

    # 跨字段校验：结合库内既有数据判断 RERANK 的 base_url 必填约束
    final_model_ids = provider_in.model_ids if provider_in.model_ids is not None else db_obj.model_ids
    final_base_url = provider_in.base_url if provider_in.base_url is not None else db_obj.base_url
    if final_model_ids:
        has_rerank = any(item.get("usage") == ModelUsage.RERANK for item in final_model_ids)
        if has_rerank and not final_base_url:
            raise ParameterException(constants.ERR_PROVIDER_RERANK_NO_URL)

    # 更新前捕获旧 model_ids，用于推断 model_id 重命名并同步到绑定的 profile
    old_model_ids = copy.deepcopy(db_obj.model_ids) if db_obj.model_ids else []

    db_obj = await provider_crud.update(db, db_obj=db_obj, obj_in=provider_in)

    synced_profile_rules = 0
    removed_profile_rules = 0
    if provider_in.is_active is False:
        removed_profile_rules = await _remove_unavailable_provider_rules(db, provider_id, [])
    elif provider_in.model_ids is not None:
        # 先把绑定该 provider 的 profile 渠道规则中被重命名的 model_id 同步更新，
        # 再清理失效规则，使重命名后的规则得以保留而非被当作删除清除
        synced_profile_rules = await _sync_provider_model_id_renames(db, provider_id, old_model_ids, db_obj.model_ids or [])
        removed_profile_rules = await _remove_unavailable_provider_rules(db, provider_id, db_obj.model_ids)

    return StandardResponse.success(
        data={
            "provider": ProviderResponse.model_validate(db_obj),
            "removed_profile_rules": removed_profile_rules,
            "synced_profile_rules": synced_profile_rules,
        },
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
    removed_profile_rules = await _remove_unavailable_provider_rules(db, provider_id, [])
    await provider_crud.remove(db, id=provider_id)
    return StandardResponse.success(
        data={"removed_profile_rules": removed_profile_rules},
        message=constants.MSG_PROVIDER_DELETED,
    )


@router.post("/test-embedding-dimension")
async def test_embedding_dimension(
    provider_id: int,
    model_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    """自动检测向量模型的输出维度。"""
    db_obj = await provider_crud.get(db, provider_id)
    if not db_obj:
        raise ResourceNotFoundException(constants.ERR_PROVIDER_NOT_FOUND)

    if not db_obj.base_url:
        raise ParameterException(constants.ERR_PROVIDER_TEST_NO_URL)

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
                message=constants.MSG_PROVIDER_TEST_SUCCESS,
                dim=dim,
            )
        else:
            raise ParameterException(constants.ERR_PROVIDER_TEST_DIMENSION_ERROR)
    except Exception as e:
        raise ParameterException(constants.ERR_PROVIDER_TEST_FAILED, message=str(e))
