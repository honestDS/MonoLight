from datetime import datetime, timezone
from typing import Optional, List, TYPE_CHECKING
from sqlmodel import SQLModel, Field, Relationship, Column, DateTime
from pydantic import ConfigDict

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.profile import Profile


class PromptBase(SQLModel):
    # Allow empty string in base to maintain compatibility with legacy database records
    name: str = Field(
        index=True, unique=True, nullable=False, min_length=1, max_length=100
    )
    content: str = Field(nullable=False)


class PromptLibrary(PromptBase, table=True):
    __tablename__ = "prompt"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    uid: Optional[int] = Field(default=None, foreign_key="user.id", nullable=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    profiles: List["Profile"] = Relationship(back_populates="prompt")
    user: Optional["User"] = Relationship(back_populates="prompts")


class PromptCreate(PromptBase):
    # Enforce strict validation only during creation
    content: str = Field(..., min_length=1)


class PromptUpdate(SQLModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    content: Optional[str] = Field(None, min_length=1)


class PromptResponse(PromptBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
