from sqlalchemy import Column, Integer, String, Text, DateTime
from app.providers.database import Base
from datetime import datetime

class PromptLibrary(Base):
    __tablename__ = 'prompt_library'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
