from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from app.providers.database import Base
from sqlalchemy.orm import relationship
from datetime import datetime


class PromptLibrary(Base):
    __tablename__ = "prompt"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    uid = Column(Integer, ForeignKey("user.id"), nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    profiles = relationship("Profile", back_populates="prompt")
    user = relationship("User", back_populates="prompts")
