from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.weixin_openclaw import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE, DEFAULT_CHANNEL_VERSION, WeixinOpenClawAdapter, WeixinOpenClawConfig, normalize_weixin_openclaw_config
from app.core.constants import (
    ERR_MESSAGE_PLATFORM_LOGIN_TOKEN_MISSING,
    ERR_MESSAGE_PLATFORM_NAME_EXISTS,
    ERR_MESSAGE_PLATFORM_NOT_FOUND,
    ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED,
    ERR_MESSAGE_PLATFORM_QRCODE_SESSION_NOT_FOUND,
    ERR_MESSAGE_PLATFORM_RECOVER_TOKEN_MISSING,
    ERR_MESSAGE_PLATFORM_UID_REQUIRED,
    ERR_MESSAGE_PLATFORM_UNSUPPORTED_TYPE,
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_USER_NOT_FOUND,
    MSG_MESSAGE_PLATFORM_CREATED,
    MSG_MESSAGE_PLATFORM_DELETED,
    MSG_MESSAGE_PLATFORM_LOGIN_STARTED,
    MSG_MESSAGE_PLATFORM_LOGIN_STATUS,
    MSG_MESSAGE_PLATFORM_RECOVERED,
    MSG_MESSAGE_PLATFORM_UPDATED,
)
from app.core.crud.account.user import user_crud
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.crud.session.session import session_crud
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.profile_validation import get_validated_profile_for_assignment
from app.core.security import get_current_user
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.models.message_platform import (
    MessagePlatform,
    MessagePlatformCreate,
    MessagePlatformLanguage,
    MessagePlatformResponse,
    MessagePlatformStatus,
    MessagePlatformType,
    MessagePlatformUpdate,
    WeixinOpenClawLoginStartResponse,
    WeixinOpenClawLoginStatusResponse,
)
from app.providers.database import get_db
from app.schemas.response import PageData, StandardResponse

router = APIRouter(prefix="/message-platforms", tags=["Message Platforms"], dependencies=[Depends(get_current_user)])


async def _get_accessible_platform(
    db: AsyncSession,
    platform_id: int,
    current_user,
    *,
    for_update: bool = False,
) -> MessagePlatform:
    if for_update:
        platform = await message_platform_crud.get_for_update(
            db,
            platform_id=platform_id,
            uid=None if current_user.is_superuser else current_user.uid,
        )
    else:
        platform = await message_platform_crud.get(db, platform_id)
    if not platform or (not current_user.is_superuser and platform.uid != current_user.uid):
        raise ResourceNotFoundException(ERR_MESSAGE_PLATFORM_NOT_FOUND)
    return platform


def _normalize_create_payload(payload: MessagePlatformCreate) -> dict:
    data = payload.model_dump()
    data["config"] = normalize_weixin_openclaw_config(data.get("config"))
    data.pop("state", None)
    data["config"].setdefault("base_url", DEFAULT_BASE_URL)
    if data["config"].get("token"):
        data["status"] = MessagePlatformStatus.CONNECTED
    return data


def _normalize_update_payload(platform: MessagePlatform, payload: MessagePlatformUpdate) -> dict:
    data = payload.model_dump(exclude_unset=True)
    data.pop("state", None)
    if "config" in data and data["config"] is not None:
        current_config = dict(platform.config or {})
        submitted_config = data["config"]
        if "session_settings" in submitted_config:
            submitted_config = {
                **submitted_config,
                "session_settings": {
                    **dict(current_config.get("session_settings") or {}),
                    **submitted_config["session_settings"],
                },
            }
        data["config"] = {**current_config, **submitted_config}
        if platform.platform_type == MessagePlatformType.WEIXIN_OPENCLAW:
            data["config"] = normalize_weixin_openclaw_config(data["config"])
    return data


def _normalize_uid(uid: str | None) -> str | None:
    normalized = str(uid or "").strip()
    return normalized or None


def _is_platform_name_integrity_error(exc: IntegrityError) -> bool:
    return is_unique_constraint_violation(
        exc,
        constraint_names=("uq_message_platform_uid_name",),
        fallback_marker_groups=(("unique constraint failed: message_platform.uid, message_platform.name",),),
    )


def _ensure_uid_for_enabled(is_enabled: bool, uid: str | None) -> None:
    if is_enabled and not _normalize_uid(uid):
        raise ParameterException(ERR_MESSAGE_PLATFORM_UID_REQUIRED)


@router.get("/types", response_model=StandardResponse)
async def get_message_platform_types():
    return StandardResponse.success(
        data={
            "platform_types": [item.value for item in MessagePlatformType],
            "languages": [item.value for item in MessagePlatformLanguage],
            "statuses": [item.value for item in MessagePlatformStatus],
        }
    )


@router.get("/list", response_model=StandardResponse)
async def list_message_platforms(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    skip = (page - 1) * size
    uid = None if current_user.is_superuser else current_user.uid
    platforms = await message_platform_crud.list_platforms(db, skip=skip, limit=size, uid=uid)
    total = await message_platform_crud.count_platforms(db, uid=uid)
    return StandardResponse.success(
        data=PageData(
            items=[MessagePlatformResponse.model_validate(platform) for platform in platforms],
            total=total,
            page=page,
            size=size,
        )
    )


@router.get("/get", response_model=StandardResponse)
async def get_message_platform(
    platform_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    platform = await _get_accessible_platform(db, platform_id, current_user)
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform))


@router.post("/create", response_model=StandardResponse)
async def create_message_platform(
    platform_in: MessagePlatformCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    submitted_uid = _normalize_uid(platform_in.uid)
    if not current_user.is_superuser and submitted_uid and submitted_uid != current_user.uid:
        raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
    payload = _normalize_create_payload(platform_in)
    if current_user.is_superuser:
        payload["uid"] = _normalize_uid(payload.get("uid") or getattr(current_user, "uid", None))
    else:
        payload["uid"] = _normalize_uid(current_user.uid)
    _ensure_uid_for_enabled(bool(payload.get("is_enabled")), payload.get("uid"))
    if payload["uid"] is not None and not await user_crud.get_by_uid(
        db,
        payload["uid"],
        lock_for_reference_write=True,
    ):
        raise ResourceNotFoundException(ERR_USER_NOT_FOUND)
    if await message_platform_crud.get_by_name(db, payload["name"], uid=payload["uid"]):
        raise ParameterException(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    if payload.get("profile_id") is not None:
        await get_validated_profile_for_assignment(
            db,
            profile_id=payload["profile_id"],
            uid=payload["uid"],
        )
    try:
        platform = await message_platform_crud.create(db, obj_in=payload)
    except IntegrityError as exc:
        await db.rollback()
        if _is_platform_name_integrity_error(exc):
            raise ParameterException(ERR_MESSAGE_PLATFORM_NAME_EXISTS) from exc
        raise
    return StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=MSG_MESSAGE_PLATFORM_CREATED)


@router.post("/update", response_model=StandardResponse)
async def update_message_platform(
    platform_id: int,
    platform_in: MessagePlatformUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    submitted_uid = _normalize_uid(platform_in.uid) if "uid" in platform_in.model_fields_set else None
    if current_user.is_superuser and submitted_uid:
        if not await user_crud.get_by_uid(db, submitted_uid, lock_for_reference_write=True):
            raise ResourceNotFoundException(ERR_USER_NOT_FOUND)
    platform = await _get_accessible_platform(db, platform_id, current_user, for_update=True)
    data = _normalize_update_payload(platform, platform_in)
    if "uid" in data:
        data["uid"] = _normalize_uid(data.get("uid"))
        if not current_user.is_superuser and data["uid"] != platform.uid:
            raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
    if data.get("config", {}).get("token"):
        data["status"] = MessagePlatformStatus.CONNECTED
        data["last_error"] = ""
    next_is_enabled = bool(data["is_enabled"]) if "is_enabled" in data else platform.is_enabled
    next_uid = data["uid"] if "uid" in data else platform.uid
    next_profile_id = data["profile_id"] if "profile_id" in data else platform.profile_id
    next_name = data["name"] if "name" in data else platform.name
    _ensure_uid_for_enabled(next_is_enabled, next_uid)
    if next_name != platform.name or next_uid != platform.uid:
        same_name = await message_platform_crud.get_by_name(db, next_name, uid=next_uid)
        if same_name and same_name.id != platform.id:
            raise ParameterException(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    if ("uid" in data or "profile_id" in data) and next_profile_id is not None:
        await get_validated_profile_for_assignment(
            db,
            profile_id=next_profile_id,
            uid=next_uid,
        )
    submitted_data = platform_in.model_dump(exclude_unset=True)
    settings = {}
    submitted_config = submitted_data.get("config")
    if isinstance(submitted_config, dict):
        submitted_session_settings = submitted_config.get("session_settings")
        if isinstance(submitted_session_settings, dict):
            settings.update(submitted_session_settings)
        if "stream_llm_request" in submitted_config:
            settings["stream_llm_request"] = submitted_config["stream_llm_request"]
    if "profile_id" in submitted_data:
        settings["profile_override_id"] = next_profile_id
    try:
        platform = await message_platform_crud.update(db, db_obj=platform, obj_in=data, commit=False)
        if next_uid and settings:
            await session_crud.update_settings_for_message_platform(
                db,
                uid=next_uid,
                message_platform_id=platform_id,
                settings=settings,
            )
        response = StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=MSG_MESSAGE_PLATFORM_UPDATED)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_platform_name_integrity_error(exc):
            raise ParameterException(ERR_MESSAGE_PLATFORM_NAME_EXISTS) from exc
        raise
    return response


@router.post("/delete", response_model=StandardResponse)
async def delete_message_platform(
    platform_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    await _get_accessible_platform(db, platform_id, current_user, for_update=True)
    await message_platform_crud.remove(db, id=platform_id)
    return StandardResponse.success(message=MSG_MESSAGE_PLATFORM_DELETED)


@router.post("/recover", response_model=StandardResponse)
async def recover_message_platform(
    platform_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    platform = await _get_accessible_platform(db, platform_id, current_user, for_update=True)
    if platform.platform_type != MessagePlatformType.WEIXIN_OPENCLAW:
        raise ParameterException(ERR_MESSAGE_PLATFORM_UNSUPPORTED_TYPE)
    if not platform.get_config_secret("token"):
        raise ParameterException(ERR_MESSAGE_PLATFORM_RECOVER_TOKEN_MISSING)
    next_status = MessagePlatformStatus.CONNECTED if platform.is_enabled else MessagePlatformStatus.DISCONNECTED
    platform = await message_platform_crud.update_runtime_state(db, platform=platform, status=next_status, last_error="", commit=False)
    response = StandardResponse.success(data=MessagePlatformResponse.model_validate(platform), message=MSG_MESSAGE_PLATFORM_RECOVERED)
    await db.commit()
    return response


@router.post("/{platform_id}/weixin-openclaw/login/start", response_model=StandardResponse)
async def start_weixin_openclaw_login(
    platform_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    platform = await _get_accessible_platform(db, platform_id, current_user)
    if platform.platform_type != MessagePlatformType.WEIXIN_OPENCLAW:
        raise ParameterException(ERR_MESSAGE_PLATFORM_UNSUPPORTED_TYPE)
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
    platform = await _get_accessible_platform(db, platform_id, current_user, for_update=True)
    platform = await message_platform_crud.update_runtime_state(
        db,
        platform=platform,
        status=MessagePlatformStatus.WAITING_LOGIN,
        state={**login_state, "qr_error": ""},
        last_error="",
        commit=False,
    )
    response = StandardResponse.success(
        message=MSG_MESSAGE_PLATFORM_LOGIN_STARTED,
        data=WeixinOpenClawLoginStartResponse(
            platform_id=platform.id,
            qrcode=login_state["qrcode"],
            qrcode_img_content=login_state["qrcode_img_content"],
            status=platform.status,
        ),
    )
    await db.commit()
    return response


@router.get("/{platform_id}/weixin-openclaw/login/status", response_model=StandardResponse)
async def get_weixin_openclaw_login_status(
    platform_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    platform = await _get_accessible_platform(db, platform_id, current_user)
    qrcode = str((platform.state or {}).get("qrcode") or "")
    if not qrcode:
        raise ParameterException(ERR_MESSAGE_PLATFORM_QRCODE_SESSION_NOT_FOUND)
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
    platform = await _get_accessible_platform(db, platform_id, current_user, for_update=True)
    qrcode_status = status_data["qrcode_status"]
    state = {"qrcode_status": qrcode_status}
    update_kwargs = {"state": state}
    if qrcode_status == "confirmed":
        token = status_data.get("token") or ""
        if not token:
            raise ParameterException(ERR_MESSAGE_PLATFORM_LOGIN_TOKEN_MISSING)
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
                "state": {**state, "qrcode": "", "qrcode_img_content": "", "qr_error": ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED},
                "last_error": ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED,
            }
        )
    else:
        update_kwargs.update({"status": MessagePlatformStatus.WAITING_LOGIN})
    platform = await message_platform_crud.update_runtime_state(db, platform=platform, commit=False, **update_kwargs)
    response = StandardResponse.success(
        message=MSG_MESSAGE_PLATFORM_LOGIN_STATUS,
        data=WeixinOpenClawLoginStatusResponse(
            platform_id=platform.id,
            status=platform.status,
            qrcode_status=qrcode_status,
            account_id=platform.account_id,
        ),
    )
    await db.commit()
    return response
