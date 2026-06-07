from app.core.middleware.auditor import (
    AuditMiddleware,
)
from app.models.message import (
    InternalMessage,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


async def audit_tool_call(
    db,
    profile: Profile,
    cfg: ProfileConfig,
    tool_name,
    args,
    messages: list[InternalMessage] = None,
    session_id: str = None,
    uid: str = None,
) -> str | None:
    return await AuditMiddleware.audit(
        db,
        profile,
        cfg,
        tool_name,
        args,
        session_id=session_id,
        uid=uid,
    )
