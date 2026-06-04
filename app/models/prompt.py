from datetime import (
    datetime,
)
from typing import (
    TYPE_CHECKING,
    Optional,
)

from pydantic import ConfigDict
from sqlmodel import (
    Column,
    DateTime,
    Field,
    Relationship,
    SQLModel,
)

from app.core.utils.dt import get_local_time

if TYPE_CHECKING:
    from app.models.profile import Profile
    from app.models.user import User


class PromptBase(SQLModel):
    # Allow empty string in base to maintain compatibility with legacy database records
    name: str = Field(
        index=True, unique=True, nullable=False, min_length=1, max_length=100
    )
    content: str = Field(nullable=False)


class PromptLibrary(PromptBase, table=True):
    __tablename__ = "prompt"
    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: int | None = Field(default=None, foreign_key="user.id", nullable=True)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
    profiles: list["Profile"] = Relationship(back_populates="prompt")
    user: Optional["User"] = Relationship(back_populates="prompts")


class PromptCreate(PromptBase):
    # Enforce strict validation only during creation
    content: str = Field(..., min_length=1)


class PromptUpdate(SQLModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    content: str | None = Field(None, min_length=1)


class PromptResponse(PromptBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
