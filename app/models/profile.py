from sqlalchemy import Column, Integer, String, Float, Boolean, JSON, ForeignKey
from app.providers.database import Base
from sqlalchemy.orm import relationship

class Profile(Base):
    __tablename__ = 'agent_profiles'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    provider_id = Column(Integer, ForeignKey('model_providers.id'))
    model_id = Column(String(100), nullable=False)
    temperature = Column(Float, default=0.7)
    top_p = Column(Float, default=1.0)
    max_tokens = Column(Integer, default=2048)
    stream = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False)
    extra_config = Column(JSON, nullable=True)

    provider = relationship('ModelProvider')