from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.weixin_openclaw import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE, DEFAULT_CHANNEL_VERSION, WeixinOpenClawAdapter, WeixinOpenClawConfig, normalize_weixin_openclaw_config
from app.api.v1.users import check_admin_privilege
from app.core import constants
from app.core.crud.message_platform import message_platform_crud
from app.core.exceptions import ParameterException, ResourceNotFoundException
from app.models.message_platform import (
    MessagePlatformCreate,
    MessagePlatformResponse,
    MessagePlatformStatus,
    MessagePlatformType,
    MessagePlatformUpdate,
    WeixinOpenClawLoginStartResponse,
    WeixinOpenClawLoginStatusResponse,
)
from app.providers.database import get_db
from app.schemas.response import PageData, StandardResponse

router = APIRouter(prefix="/message-platforms", tags=["Message Platforms"], dependencies=[Depends(check_admin_privilege)])


async def get_admin_from_request(admin=Depends(check_admin_privilege)):
    # 此处需要从请求中获取 admin 信息 但为了减少重复代码 将权限校验放在路由级
    # 消费方可直接使用该函数获取 admin 信息
    return admin


def _normalize_create_payload(payload: MessagePlatformCreate) -> dict:
    data = payload.model_dump()
    data["config"] = normalize_weixin_openclaw_config(data.get("config"))
    data.pop("state", None)
    data["config"].setdefault("base_url", DEFAULT_BASE_URL)
    if data["config"].get("token"):
        data["status"] = MessagePlatformStatus.CONNECTED
    return data


def _normalize_update_payload(platform_type: MessagePlatformType, payload: MessagePlatformUpdate) -> dict:
    data = payload.model_dump(exclude_unset=True)
    data.pop("state", None)
    if "config" in data and data["config"] is not None and platform_type == MessagePlatformType.WEIXIN_OPENCLAW:
        data["config"] = normalize_weixin_openclaw_config(data["config"])
    return data


def _normalize_uid(uid: str | None) -> str | None:
    normalized = str(uid or "").strip()
    return normalized or None


def _ensure_uid_for_enabled(is_enabled: bool, uid: str | None) -> None:
    if is_enabled and not _normalize_uid(uid):
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_UID_REQUIRED)


@router.get("/types", response_model=StandardResponse)
async def get_message_platform_types():
    return StandardResponse.success(
        data={
            "platform_types": [item.value for item in MessagePlatformType],
            "statuses": [item.value for item in MessagePlatformStatus],
        }
    )


@router.get("/list", response_model=StandardResponse)
async def list_message_platforms(page: int = 1, size: int = 10, db: AsyncSession = Depends(get_db)):
    skip = (page - 1) * size
    platforms = await message_platform_crud.list_platforms(db, skip=skip, limit=size)
    total = await message_platform_crud.count_platforms(db)
    return StandardResponse.success(
        data=PageData(
            items=[MessagePlatformResponse.model_validate(platform) for platform in platforms],
            total=total,
            page=page,
            size=size,
        )
    )


@router.get("/get", response_model=StandardResponse)
async def get_message_platform(platform_id: int, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform))


@router.post("/create", response_model=StandardResponse)
async def create_message_platform(platform_in: MessagePlatformCreate, db: AsyncSession = Depends(get_db), admin=Depends(get_admin_from_request)):
    if await message_platform_crud.get_by_name(db, platform_in.name):
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    payload = _normalize_create_payload(platform_in)
    payload["uid"] = _normalize_uid(payload.get("uid") or getattr(admin, "uid", None))
    _ensure_uid_for_enabled(bool(payload.get("is_enabled")), payload.get("uid"))
    platform = await message_platform_crud.create(db, obj_in=payload)
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=constants.MSG_MESSAGE_PLATFORM_CREATED)


@router.post("/update", response_model=StandardResponse)
async def update_message_platform(platform_id: int, platform_in: MessagePlatformUpdate, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    if platform_in.name and platform_in.name != platform.name:
        same_name = await message_platform_crud.get_by_name(db, platform_in.name)
        if same_name:
            raise ParameterException(constants.ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    data = _normalize_update_payload(platform.platform_type, platform_in)
    if "uid" in data:
        data["uid"] = _normalize_uid(data.get("uid"))
    if "config" in data:
        data["config"] = {**dict(platform.config or {}), **data["config"]}
    if data.get("config", {}).get("token"):
        data["status"] = MessagePlatformStatus.CONNECTED
        data["last_error"] = ""
    next_is_enabled = bool(data["is_enabled"]) if "is_enabled" in data else platform.is_enabled
    next_uid = data["uid"] if "uid" in data else platform.uid
    _ensure_uid_for_enabled(next_is_enabled, next_uid)
    platform = await message_platform_crud.update(db, db_obj=platform, obj_in=data)
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=constants.MSG_MESSAGE_PLATFORM_UPDATED)


@router.post("/delete", response_model=StandardResponse)
async def delete_message_platform(platform_id: int, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    await message_platform_crud.remove(db, id=platform_id)
    return StandardResponse.success(message=constants.MSG_MESSAGE_PLATFORM_DELETED)


@router.post("/recover", response_model=StandardResponse)
async def recover_message_platform(platform_id: int, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    if platform.platform_type != MessagePlatformType.WEIXIN_OPENCLAW:
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_UNSUPPORTED_TYPE)
    if not platform.get_config_secret("token"):
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_RECOVER_TOKEN_MISSING)
    next_status = MessagePlatformStatus.CONNECTED if platform.is_enabled else MessagePlatformStatus.DISCONNECTED
    platform = await message_platform_crud.update_runtime_state(db, platform=platform, status=next_status, last_error="")
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=constants.MSG_MESSAGE_PLATFORM_RECOVERED)


@router.post("/{platform_id}/weixin-openclaw/login/start", response_model=StandardResponse)
async def start_weixin_openclaw_login(platform_id: int, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    if platform.platform_type != MessagePlatformType.WEIXIN_OPENCLAW:
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_UNSUPPORTED_TYPE)
    config = normalize_weixin_openclaw_config(platform.config)
    adapter = WeixinOpenClawAdapter(
        WeixinOpenClawConfig(
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            cdn_base_url=str(config["cdn_base_url"]),
            bot_type=DEFAULT_BOT_TYPE,
            channel_version=DEFAULT_CHANNEL_VERSION,
            api_timeout_ms=config["api_timeout_ms"],
            long_poll_timeout_ms=config["long_poll_timeout_ms"],
            poll_interval_ms=config["poll_interval_ms"],
            max_inbound_media_size_mb=config["max_inbound_media_size_mb"],
            merge_single_poll_messages=config["merge_single_poll_messages"],
        )
    )
    try:
        login_state = await adapter.start_login_session()
    finally:
        await adapter.close()
    platform = await message_platform_crud.update_runtime_state(
        db,
        platform=platform,
        status=MessagePlatformStatus.WAITING_LOGIN,
        state={**login_state, "qr_error": ""},
        last_error="",
    )
    return StandardResponse.success(
        message=constants.MSG_MESSAGE_PLATFORM_LOGIN_STARTED,
        data=WeixinOpenClawLoginStartResponse(
            platform_id=platform.id,
            qrcode=login_state["qrcode"],
            qrcode_img_content=login_state["qrcode_img_content"],
            status=platform.status,
        ),
    )


@router.get("/{platform_id}/weixin-openclaw/login/status", response_model=StandardResponse)
async def get_weixin_openclaw_login_status(platform_id: int, db: AsyncSession = Depends(get_db)):
    platform = await message_platform_crud.get(db, platform_id)
    if not platform:
        raise ResourceNotFoundException(constants.ERR_MESSAGE_PLATFORM_NOT_FOUND)
    qrcode = str((platform.state or {}).get("qrcode") or "")
    if not qrcode:
        raise ParameterException(constants.ERR_MESSAGE_PLATFORM_QRCODE_SESSION_NOT_FOUND)
    config = normalize_weixin_openclaw_config(platform.config)
    adapter = WeixinOpenClawAdapter(
        WeixinOpenClawConfig(
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            cdn_base_url=str(config["cdn_base_url"]),
            bot_type=DEFAULT_BOT_TYPE,
            channel_version=DEFAULT_CHANNEL_VERSION,
            api_timeout_ms=config["api_timeout_ms"],
            long_poll_timeout_ms=config["long_poll_timeout_ms"],
            poll_interval_ms=config["poll_interval_ms"],
            max_inbound_media_size_mb=config["max_inbound_media_size_mb"],
            merge_single_poll_messages=config["merge_single_poll_messages"],
        )
    )
    try:
        status_data = await adapter.poll_qrcode_status(qrcode)
    finally:
        await adapter.close()
    qrcode_status = status_data["qrcode_status"]
    state = {"qrcode_status": qrcode_status}
    update_kwargs = {"state": state}
    if qrcode_status == "confirmed":
        token = status_data.get("token") or ""
        if not token:
            raise ParameterException(constants.ERR_MESSAGE_PLATFORM_LOGIN_TOKEN_MISSING)
        config_update = {"token": token}
        if status_data.get("base_url"):
            config_update["base_url"] = status_data["base_url"]
        update_kwargs.update(
            {
                "status": MessagePlatformStatus.CONNECTED,
                "config": config_update,
                "state": {**state, "qrcode": "", "qrcode_img_content": "", "qr_error": ""},
                "account_id": status_data.get("account_id") or None,
                "last_error": "",
            }
        )
    elif qrcode_status == "expired":
        update_kwargs.update(
            {
                "status": MessagePlatformStatus.ERROR,
                "state": {**state, "qrcode": "", "qrcode_img_content": "", "qr_error": constants.ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED},
                "last_error": constants.ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED,
            }
        )
    else:
        update_kwargs.update({"status": MessagePlatformStatus.WAITING_LOGIN})
    platform = await message_platform_crud.update_runtime_state(db, platform=platform, **update_kwargs)
    return StandardResponse.success(
        message=constants.MSG_MESSAGE_PLATFORM_LOGIN_STATUS,
        data=WeixinOpenClawLoginStatusResponse(
            platform_id=platform.id,
            status=platform.status,
            qrcode_status=qrcode_status,
            account_id=platform.account_id,
        ),
    )
