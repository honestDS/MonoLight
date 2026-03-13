from sqlalchemy.orm import Mapped, mapped_column
from app.providers.database import Base
from enum import Enum
import enum

class ProviderType(str, enum.Enum):
    OPENAI = 'OPENAI'
    GEMINI = 'GEMINI'

class ModelProvider(Base):
    __tablename__ = 'model_providers'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, index=True)
    provider_type: Mapped[ProviderType] = mapped_column(default=ProviderType.OPENAI)
    api_key: Mapped[str] = mapped_column()
    base_url: Mapped[str] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
