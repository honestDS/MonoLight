from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile

DispatchMode = Literal["interactive", "scheduled", "background"]


@dataclass(slots=True)
class DispatchContext:
    mode: DispatchMode
    source: str
    uid: str
    session_id: str
    profile: Profile
    db: AsyncSession | None = None
    tool_call_id: str | None = None
    task_id: int | None = None
    allowed_knowledge_base_ids: list[int] = field(default_factory=list)

    @property
    def is_background(self) -> bool:
        return self.mode == "background"


def build_background_dispatch_context(
    *,
    uid: str,
    session_id: str,
    profile: Profile,
    db: AsyncSession | None = None,
    tool_call_id: str | None = None,
    task_id: int | None = None,
    source: str = "background_task",
    allowed_knowledge_base_ids: list[int] | None = None,
) -> DispatchContext:
    return DispatchContext(
        mode="background",
        source=source,
        uid=uid,
        session_id=session_id,
        profile=profile,
        db=db,
        tool_call_id=tool_call_id,
        task_id=task_id,
        allowed_knowledge_base_ids=allowed_knowledge_base_ids or [],
    )


def build_dispatch_context(
    *,
    mode: DispatchMode,
    source: str,
    uid: str,
    session_id: str,
    profile: Profile,
    db: AsyncSession | None = None,
    tool_call_id: str | None = None,
    task_id: int | None = None,
    allowed_knowledge_base_ids: list[int] | None = None,
) -> DispatchContext:
    return DispatchContext(
        mode=mode,
        source=source,
        uid=uid,
        session_id=session_id,
        profile=profile,
        db=db,
        tool_call_id=tool_call_id,
        task_id=task_id,
        allowed_knowledge_base_ids=allowed_knowledge_base_ids or [],
    )
