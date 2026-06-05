from datetime import datetime

from sqlmodel import Field, SQLModel

from app.core.utils.dt import get_local_time


class ActiveSession(SQLModel, table=True):
    """
    活跃会话追踪表，用于分布式环境下的任务锁。
    session_id 作为主键，确保同一时间只有一个任务能处理该会话。
    """
    __tablename__ = "active_session"

    session_id: str = Field(primary_key=True, max_length=100)
    created_at: datetime = Field(
        default_factory=get_local_time,
        description="锁创建时间，可用于清理过期锁"
    )
