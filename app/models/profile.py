from sqlalchemy import Column, Integer, String, Float, Boolean, JSON, ForeignKey
from app.providers.database import Base
from sqlalchemy.orm import relationship

class Profile(Base):
    __tablename__ = 'agent_profiles'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    uid = Column(Integer, ForeignKey('users.id'), nullable=True)
    provider_id = Column(Integer, ForeignKey('model_providers.id'))
    prompt_id = Column(Integer, ForeignKey('prompt_library.id'), nullable=True)
    model_id = Column(String(100), nullable=False)
    temperature = Column(Float, default=0.7)
    top_p = Column(Float, default=1.0)
    max_tokens = Column(Integer, default=2048)  # 模型单次回复的最大 Token 数。若设置为 <= 0 则不限制（由供应商自行决定）。
    stream = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False)
    extra_config = Column(JSON, nullable=True)
    context_window_k = Column(Integer, default=4)  # 会话上下文历史窗口大小（单位 K，控制输入长度）

    provider = relationship('ModelProvider')
    prompt = relationship('PromptLibrary')
    user = relationship('User', back_populates='profiles')
