import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CHANNEL_NAME_EXISTS,
    ERR_SETUP_CONFLICT,
    ERR_SETUP_STATE_UPDATE_FAILED,
    ERR_USER_NAME_EXISTS,
)
from app.core.crud.channel import channel_crud
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.crud.user import user_crud
from app.core.exceptions import ParameterException, ServerException
from app.core.profile_validation import validate_channel_configs
from app.core.security import create_access_token, get_password_hash
from app.models.channel import (
    ChannelCreate,
    ChannelModelItem,
    ModelUsage,
)
from app.models.profile import ProfileConfig
from app.schemas.setup import (
    SetupCompleteRequest,
    SetupCompleteResult,
)


async def complete_setup(db: AsyncSession, request: SetupCompleteRequest) -> SetupCompleteResult:
    try:
        if not await system_setting_crud.claim_setup(db):
            raise ParameterException(ERR_SETUP_CONFLICT, code=409)

        if await user_crud.get_by_username(db, request.admin.username):
            raise ParameterException(ERR_USER_NAME_EXISTS)
        if await channel_crud.get_by_name(db, request.channel.name):
            raise ParameterException(ERR_CHANNEL_NAME_EXISTS)

        admin_uid = uuid.uuid4().hex
        admin = await user_crud.create(
            db,
            obj_in=request.admin,
            update_dict={
                "uid": admin_uid,
                "hashed_password": get_password_hash(request.admin.password),
                "is_superuser": True,
                "is_active": True,
            },
            commit=False,
        )

        model_item = ChannelModelItem(
            model_id=request.channel.model_id,
            usage=ModelUsage.CHAT,
            protocol=request.channel.protocol,
            image_understanding=request.channel.image_understanding,
            audio_understanding=request.channel.audio_understanding,
            video_understanding=request.channel.video_understanding,
            context_window_k=request.channel.context_window_k,
            temperature=request.channel.temperature,
            top_p=request.channel.top_p,
            max_tokens=request.channel.max_tokens,
            description=request.channel.description,
            advanced_settings=request.channel.advanced_settings,
        )
        channel = await channel_crud.create_with_plain_api_key(
            db,
            obj_in=ChannelCreate(
                name=request.channel.name,
                api_key=request.channel.api_key,
                base_url=request.channel.base_url,
                http_proxy=request.channel.http_proxy,
                model_ids=[model_item.model_dump(mode="json")],
            ),
            commit=False,
        )

        prompt = await prompt_crud.get_by_name(db, name="default", uid=None)
        if prompt is None:
            prompt = await prompt_crud.create(
                db,
                obj_in={"name": "default", "content": "", "uid": None},
                commit=False,
            )

        profile_config = ProfileConfig(
            channel={
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": channel.id,
                            "model_id": request.channel.model_id,
                            "priority": 1,
                            "weight": 100,
                            "is_enabled": True,
                        }
                    ]
                },
                "context_summary_channel": {
                    "rules": [
                        {
                            "channel_id": channel.id,
                            "model_id": request.channel.model_id,
                            "priority": 1,
                            "weight": 100,
                            "is_enabled": True,
                        }
                    ]
                },
            },
            security={},
            tool={},
            other={},
        )
        await validate_channel_configs(db, profile_config.channel.model_dump())

        profile = await profile_crud.create(
            db,
            obj_in={
                "name": request.profile.name,
                "uid": admin.uid,
                "prompt_id": prompt.id,
                "configs": profile_config.model_dump(),
                "is_default": True,
            },
            commit=False,
        )

        if not await system_setting_crud.set_setup_admin_uid(db, admin_uid=admin.uid):
            raise ServerException(ERR_SETUP_STATE_UPDATE_FAILED)
        if not await system_setting_crud.complete_setup(db):
            raise ServerException(ERR_SETUP_STATE_UPDATE_FAILED)

        await db.commit()
        access_token = create_access_token({"sub": admin.username})
        return SetupCompleteResult(
            access_token=access_token,
            token_type="bearer",
            profile_id=profile.id,
            channel_id=channel.id,
        )
    except Exception:
        await db.rollback()
        raise
