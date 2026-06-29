from datetime import (
    datetime,
)

from pydantic import ConfigDict
from sqlmodel import (
    Column,
    DateTime,
    Field,
    SQLModel,
)

from app.core.utils.time import get_local_time


class PromptBase(SQLModel):
    # Allow empty string in base to maintain compatibility with legacy database records
    uid: str | None = Field(default=None, index=True, max_length=50)
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100)
    content: str = Field(nullable=False)


class PromptLibrary(PromptBase, table=True):
    __tablename__ = "prompt"
    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )


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
