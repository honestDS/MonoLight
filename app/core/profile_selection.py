from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message_platform.platform import message_platform_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.models.profile import Profile


def _is_valid_profile_id(profile_id: int | None) -> bool:
    return isinstance(profile_id, int) and not isinstance(profile_id, bool) and profile_id > 0


async def _get_owned_profile(db: AsyncSession, *, uid: str, profile_id: int | None) -> Profile | None:
    if not _is_valid_profile_id(profile_id):
        return None
    profile = await profile_crud.get_with_relations(db, profile_id)
    return profile if profile is not None and profile.uid == uid else None


async def resolve_profile_for_session(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    message_platform_id: int | None = None,
) -> Profile | None:
    session = await session_crud.get_by_session_id(db, session_id)
    if session is not None and session.uid == uid:
        profile = await _get_owned_profile(db, uid=uid, profile_id=getattr(session, "profile_override_id", None))
        if profile is not None:
            return profile

    if message_platform_id is not None:
        platform = await message_platform_crud.get(db, message_platform_id)
        if platform is not None and platform.uid == uid:
            profile = await _get_owned_profile(db, uid=uid, profile_id=getattr(platform, "profile_id", None))
            if profile is not None:
                return profile

    return await profile_crud.get_default(db, uid=uid)
