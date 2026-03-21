from sqlalchemy import JSON, Boolean, Column, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from app.providers.database import Base


class Profile(Base):
    __tablename__ = "profile"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    provider_id = Column(Integer, ForeignKey("provider.id"))
    prompt_id = Column(Integer, ForeignKey("prompt.id"), nullable=True)
    model_id = Column(String(100), nullable=False)
    temperature = Column(Float, default=0.7)
    top_p = Column(Float, default=1.0)
    max_tokens = Column(Integer, default=2048)
    stream = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False)
    extra_config = Column(JSON, nullable=True)
    context_window_k = Column(Integer, default=4)

    # 审计相关配置
    audit_provider_id = Column(Integer, ForeignKey("provider.id"), nullable=True)
    audit_model_id = Column(String(100), nullable=True)
    audit_threshold = Column(Integer, default=5)

    provider = relationship("ModelProvider", foreign_keys=[provider_id])
    audit_provider = relationship("ModelProvider", foreign_keys=[audit_provider_id])
    prompt = relationship("PromptLibrary")
