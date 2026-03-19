from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.providers.database import Base
from sqlalchemy.orm import relationship


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String(50), unique=True, index=True, nullable=False)  # 外部平台关联 ID
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=True)  # 若仅通过外部鉴权可为空
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 关联
    profiles = relationship("Profile", back_populates="user")
    prompts = relationship("PromptLibrary", back_populates="user")
