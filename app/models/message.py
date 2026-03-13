from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from app.providers.database import Base
from datetime import datetime

class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True)
    role = Column(String(20))
    content = Column(String)
    profile_id = Column(Integer, ForeignKey('agent_profiles.id'))
    created_at = Column(DateTime, default=datetime.now)
