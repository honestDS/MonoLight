from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from app.providers.database import Base


class Profile(Base):
    __tablename__ = "profile"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    provider_id = Column(Integer, ForeignKey("provider.id"))
    prompt_id = Column(Integer, ForeignKey("prompt.id"), nullable=True)
    is_active = Column(Boolean, default=False)
    configs = Column(JSON, nullable=False)

    provider = relationship("ModelProvider", foreign_keys=[provider_id])
    prompt = relationship("PromptLibrary")
