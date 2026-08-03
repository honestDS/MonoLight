"""Channel API：渠道管理架构适配版

CRUD 支持 model_ids 字段；移除 usage 字段
"""

import copy
import json
import re
from enum import StrEnum
from time import perf_counter

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, field_validator
from pydantic import Field as PydanticField
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_model_protection import (
    assert_channel_model_identity_update_allowed,
    assert_channel_not_referenced,
)
from app.core.constants import (
    ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS,
    ERR_CHANNEL_BASE_URL_SCHEME,
    ERR_CHANNEL_CHAT_TEST_EMPTY_RESPONSE,
    ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID,
    ERR_CHANNEL_CHAT_TEST_PROMPT_REQUIRED,
    ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE,
    ERR_CHANNEL_MODEL_IDS_ITEM_INVALID,
    ERR_CHANNEL_MODEL_LIST_FAILED,
    ERR_CHANNEL_MODEL_LIST_NO_API_KEY,
    ERR_CHANNEL_MODEL_LIST_NO_URL,
    ERR_CHANNEL_MODEL_NOT_FOUND,
    ERR_CHANNEL_MODEL_PROTOCOL_REQUIRED,
    ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID,
    ERR_CHANNEL_NAME_EXISTS,
    ERR_CHANNEL_NOT_FOUND,
    ERR_CHANNEL_TEST_DIMENSION_ERROR,
    ERR_CHANNEL_TEST_FAILED,
    ERR_CHANNEL_TEST_NO_URL,
    ERR_ONLY_ADMIN_ALLOWED,
    MSG_CHANNEL_CHAT_TEST_SUCCESS,
    MSG_CHANNEL_CREATED,
    MSG_CHANNEL_DELETED,
    MSG_CHANNEL_IMAGE_GENERATION_TEST_SUCCESS,
    MSG_CHANNEL_MODEL_LIST_SUCCESS,
    MSG_CHANNEL_TEST_SUCCESS,
    MSG_CHANNEL_UPDATED,
)
from app.core.crud.channel import channel_crud
from app.core.exceptions import (
    BaseBusinessException,
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.core.utils.channel_profile_sync import (
    _clear_unavailable_audit_model_refs,
    _remove_unavailable_channel_rules,
    _sync_audit_model_id_renames,
    _sync_channel_model_id_renames,
)
from app.core.utils.http_proxy import get_channel_http_proxy, normalize_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.models.channel import (
    MODEL_PROTOCOLS_BY_USAGE,
    ChannelCreate,
    ChannelListResponse,
    ChannelModelAdvancedSettings,
    ChannelModelIdsNormalizationError,
    ChannelResponse,
    ChannelUpdate,
    ImageGenerationQuality,
    ImageGenerationSize,
    ModelProtocol,
    ModelUsage,
    normalize_channel_model_ids,
    resolve_model_protocol,
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


class ChannelHTTPProxyRequest(BaseModel):
    http_proxy: str | None = None

    @field_validator("http_proxy", mode="before")
    @classmethod
    def validate_http_proxy(cls, value: object) -> str | None:
        return normalize_http_proxy(value)


class ChannelModelListRequest(ChannelHTTPProxyRequest):
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = PydanticField(30.0, gt=0, le=120)


class ChannelChatTestMode(StrEnum):
    NON_STREAM = "non_stream"
    STREAM = "stream"


class ChannelChatTestRequest(ChannelHTTPProxyRequest):
    prompt: str
    protocol: ModelProtocol | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    temperature: float | None = PydanticField(None, ge=0, le=2.0)
    top_p: float | None = PydanticField(None, ge=0, le=1.0)
    max_tokens: int | None = PydanticField(None, ge=0)
    timeout: float = PydanticField(60.0, gt=0, le=600)
    advanced_settings: ChannelModelAdvancedSettings = PydanticField(default_factory=ChannelModelAdvancedSettings)
    test_mode: ChannelChatTestMode = ChannelChatTestMode.NON_STREAM


class ChannelImageGenerationTestRequest(ChannelHTTPProxyRequest):
    protocol: ModelProtocol | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    size: ImageGenerationSize = ImageGenerationSize.SIZE_1024X1024
    quality: ImageGenerationQuality | None = ImageGenerationQuality.AUTO
    timeout: float = PydanticField(60.0, gt=0, le=600)
    advanced_settings: ChannelModelAdvancedSettings = PydanticField(default_factory=ChannelModelAdvancedSettings)


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
    return current_user


def _clean_channel_model_ids_for_usage(model_ids: list[dict] | None) -> list[dict]:
    normalized_model_ids = []
    for item in model_ids or []:
        normalized_item = copy.deepcopy(item)
        if str(normalized_item.get("usage")) != ModelUsage.IMAGE_GENERATION.value:
            normalized_item.pop("size", None)
            normalized_item.pop("quality", None)
        normalized_model_ids.append(normalized_item)
    return normalized_model_ids


def _prepare_channel_model_ids(model_ids: list[dict] | None) -> tuple[list[dict] | None, dict | None]:
    try:
        return normalize_channel_model_ids(_clean_channel_model_ids_for_usage(model_ids)), None
    except ChannelModelIdsNormalizationError as exc:
        return None, {"index": exc.index, "error": exc.error}


router = APIRouter(prefix="/channels", tags=["Channels"], dependencies=[Depends(get_current_user)])


@router.post("/create", response_model=StandardResponse)
async def create_channel(
    channel_in: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    channel_in.model_ids, normalization_error = _prepare_channel_model_ids(channel_in.model_ids)
    if normalization_error:
        return StandardResponse.error(code=422, message=ERR_CHANNEL_MODEL_IDS_ITEM_INVALID, **normalization_error)
    validation_error, validation_kwargs = validate_channel_model_ids(channel_in.model_ids)
    if validation_error:
        return StandardResponse.error(code=422, message=validation_error, **validation_kwargs)

    if channel_in.base_url and not re.match(r"^https?://", channel_in.base_url):
        return StandardResponse.error(code=422, message=ERR_CHANNEL_BASE_URL_SCHEME)

    if channel_in.model_ids and not channel_in.base_url:
        return StandardResponse.error(code=422, message=ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)

    if await channel_crud.get_by_name(db, channel_in.name):
        raise ParameterException(ERR_CHANNEL_NAME_EXISTS)

    db_obj = await channel_crud.create_with_plain_api_key(db, obj_in=channel_in)

    return StandardResponse.success(
        data=ChannelResponse.model_validate(db_obj),
        message=MSG_CHANNEL_CREATED,
    )


@router.get("/types", response_model=StandardResponse)
async def get_channel_types():
    return StandardResponse.success(
        data={
            "model_protocols": {usage.value: [protocol.value for protocol in protocols] for usage, protocols in MODEL_PROTOCOLS_BY_USAGE.items()},
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
        raise ResourceNotFoundException(ERR_CHANNEL_NOT_FOUND)
    return StandardResponse.success(data=ChannelResponse.model_validate(db_obj))


@router.post("/models", response_model=StandardResponse)
async def list_channel_models(
    payload: ChannelModelListRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    """检测渠道模型列表。"""
    api_key = payload.api_key
    base_url = payload.base_url

    if not base_url:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_API_KEY)

    try:
        models = await LLMClient.list_models(
            api_key=api_key,
            base_url=base_url,
            timeout=payload.timeout,
            http_proxy=payload.http_proxy,
        )
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_FAILED, detail=str(e)) from e

    return StandardResponse.success(
        data={"models": models},
        message=MSG_CHANNEL_MODEL_LIST_SUCCESS,
    )


@router.post("/test-chat", response_model=StandardResponse)
async def test_channel_chat(
    payload: ChannelChatTestRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    protocol = payload.protocol
    api_key = payload.api_key
    base_url = payload.base_url
    model_id = payload.model_id

    if not protocol:
        raise ParameterException(ERR_CHANNEL_MODEL_PROTOCOL_REQUIRED)
    if protocol not in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]:
        raise ParameterException(ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID)
    if not base_url:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_API_KEY)
    if not model_id or not model_id.strip():
        raise ParameterException(ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID)
    if not payload.prompt.strip():
        raise ParameterException(ERR_CHANNEL_CHAT_TEST_PROMPT_REQUIRED)

    request_kwargs = {
        "protocol": protocol.value.lower(),
        "api_key": api_key,
        "base_url": base_url,
        "model_id": model_id.strip(),
        "messages": [InternalMessage(role=MessageRole.USER, content=payload.prompt)],
        "temperature": payload.temperature if payload.temperature is not None else 0.7,
        "max_tokens": payload.max_tokens or 0,
        "top_p": payload.top_p,
        "timeout": payload.timeout,
        "http_proxy": payload.http_proxy,
        "custom_headers": payload.advanced_settings.custom_headers,
    }
    first_char_latency_ms = None
    request_start = 0.0

    async def on_content(content: str) -> None:
        nonlocal first_char_latency_ms
        if isinstance(content, str) and content and first_char_latency_ms is None:
            first_char_latency_ms = round((perf_counter() - request_start) * 1000, 2)

    try:
        request_start = perf_counter()
        if payload.test_mode == ChannelChatTestMode.NON_STREAM:
            response = await LLMClient.generate(**request_kwargs)
            latency_ms = round((perf_counter() - request_start) * 1000, 2)
        else:
            response = await LLMClient.generate_with_stream_callback(**request_kwargs, on_content=on_content)
            total_latency_ms = round((perf_counter() - request_start) * 1000, 2)
        content = response.message.content
        if isinstance(content, str):
            reply = content.strip()
        else:
            reply = json.dumps(content, ensure_ascii=False) if content else ""
        if not reply:
            raise ParameterException(ERR_CHANNEL_CHAT_TEST_EMPTY_RESPONSE)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e

    timing_data = (
        {"latency_ms": latency_ms}
        if payload.test_mode == ChannelChatTestMode.NON_STREAM
        else {
            "first_char_latency_ms": first_char_latency_ms,
            "total_latency_ms": total_latency_ms,
        }
    )
    return StandardResponse.success(
        data={"model": response.model, "reply": reply, "usage": response.usage, "test_mode": payload.test_mode.value, **timing_data},
        message=MSG_CHANNEL_CHAT_TEST_SUCCESS,
    )


@router.post("/test-image-generation", response_model=StandardResponse)
async def test_channel_image_generation(
    payload: ChannelImageGenerationTestRequest = Body(...),
    _admin: dict = Depends(check_admin_privilege),
):
    api_key = payload.api_key
    base_url = payload.base_url
    model_id = payload.model_id
    protocol = payload.protocol

    if not protocol:
        raise ParameterException(ERR_CHANNEL_MODEL_PROTOCOL_REQUIRED)
    if protocol not in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.IMAGE_GENERATION]:
        raise ParameterException(ERR_CHANNEL_MODEL_PROTOCOL_USAGE_INVALID)
    if not base_url:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_URL)
    if not api_key:
        raise ParameterException(ERR_CHANNEL_MODEL_LIST_NO_API_KEY)
    if not model_id or not model_id.strip():
        raise ParameterException(ERR_CHANNEL_CHAT_TEST_NO_MODEL_ID)

    try:
        request_start = perf_counter()
        response = await ImageGenerationClient.generate_image(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id.strip(),
            protocol=protocol.value.lower(),
            prompt="A simple red apple on a white background.",
            size=payload.size,
            n=1,
            quality=payload.quality,
            timeout=payload.timeout,
            http_proxy=payload.http_proxy,
            custom_headers=payload.advanced_settings.custom_headers,
        )
        latency_ms = round((perf_counter() - request_start) * 1000, 2)
        images = response.get("data") if isinstance(response, dict) else None
        if not isinstance(images, list) or not images:
            raise ParameterException(ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE)
        first_image = images[0] if isinstance(images[0], dict) else {}
        if not first_image.get("url") and not first_image.get("b64_json"):
            raise ParameterException(ERR_CHANNEL_IMAGE_GENERATION_TEST_EMPTY_RESPONSE)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e

    return StandardResponse.success(
        data={
            "model": response.get("model", model_id.strip()) if isinstance(response, dict) else model_id.strip(),
            "image": first_image,
            "latency_ms": latency_ms,
        },
        message=MSG_CHANNEL_IMAGE_GENERATION_TEST_SUCCESS,
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
        raise ResourceNotFoundException(ERR_CHANNEL_NOT_FOUND)

    if channel_in.name and channel_in.name != db_obj.name:
        if await channel_crud.get_by_name(db, channel_in.name):
            raise ParameterException(ERR_CHANNEL_NAME_EXISTS)

    # 校验 model_ids 合法性（如果传入）
    if channel_in.model_ids is not None:
        channel_in.model_ids, normalization_error = _prepare_channel_model_ids(channel_in.model_ids)
        if normalization_error:
            return StandardResponse.error(code=422, message=ERR_CHANNEL_MODEL_IDS_ITEM_INVALID, **normalization_error)
        validation_error, validation_kwargs = validate_channel_model_ids(channel_in.model_ids)
        if validation_error:
            return StandardResponse.error(code=422, message=validation_error, **validation_kwargs)

    if channel_in.base_url and not re.match(r"^https?://", channel_in.base_url):
        return StandardResponse.error(code=422, message=ERR_CHANNEL_BASE_URL_SCHEME)

    # 跨字段校验：所有可调用模型类型都依赖 base_url 拼接供应商接口路径。
    final_model_ids = channel_in.model_ids if channel_in.model_ids is not None else db_obj.model_ids
    final_base_url = channel_in.base_url if "base_url" in channel_in.model_fields_set else db_obj.base_url
    if final_model_ids and not final_base_url:
        return StandardResponse.error(code=422, message=ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)

    # 更新前捕获旧 model_ids，用于推断 model_id 重命名并同步到绑定的 profile
    old_model_ids = copy.deepcopy(db_obj.model_ids) if db_obj.model_ids else []
    if channel_in.model_ids is not None:
        await assert_channel_model_identity_update_allowed(
            db,
            channel_id=channel_id,
            old_model_ids=old_model_ids,
            new_model_ids=channel_in.model_ids,
        )

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
        message=MSG_CHANNEL_UPDATED,
    )


@router.post("/delete")
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_obj = await channel_crud.get(db, channel_id)
    if not db_obj:
        raise ResourceNotFoundException(ERR_CHANNEL_NOT_FOUND)

    await assert_channel_not_referenced(db, channel_id=channel_id)

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
        message=MSG_CHANNEL_DELETED,
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
        raise ResourceNotFoundException(ERR_CHANNEL_NOT_FOUND)

    if not db_obj.base_url:
        raise ParameterException(ERR_CHANNEL_TEST_NO_URL)

    model_entry = next(
        (item for item in db_obj.model_ids or [] if item.get("model_id") == model_id and str(item.get("usage")) == ModelUsage.EMBEDDING.value and item.get("is_enabled", True)),
        None,
    )
    if model_entry is None:
        raise ParameterException(ERR_CHANNEL_MODEL_NOT_FOUND)

    try:
        embedding_response = await EmbeddingClient.get_embeddings(
            api_key=db_obj.get_decrypted_api_key(),
            base_url=db_obj.base_url,
            model_id=model_id,
            input_texts=["dimension test"],
            protocol=resolve_model_protocol(model_entry),
            http_proxy=get_channel_http_proxy(db_obj),
            custom_headers=get_model_custom_headers(model_entry),
        )
        if "data" in embedding_response and len(embedding_response["data"]) > 0:
            dimension = len(embedding_response["data"][0]["embedding"])
            return StandardResponse.success(
                data={"dimension": dimension},
                message=MSG_CHANNEL_TEST_SUCCESS,
                dim=dimension,
            )
        else:
            raise ParameterException(ERR_CHANNEL_TEST_DIMENSION_ERROR)
    except ParameterException:
        raise
    except BaseBusinessException as e:
        detail = t(e.message, default=e.message, **e.kwargs)
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=detail) from e
    except Exception as e:
        raise ParameterException(ERR_CHANNEL_TEST_FAILED, detail=str(e)) from e
