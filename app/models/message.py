from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.providers.database import Base


class Message(Base):
    __tablename__ = "message"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True)
    uid = Column(String(100), index=True)
    role = Column(String(20))
    content = Column(String)
    profile_id = Column(Integer, ForeignKey("profile.id"))
    created_at = Column(DateTime, default=datetime.now)
