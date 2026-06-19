"""渠道加权轮询游标：持久化轮询位置，支持多 worker 共享

加权轮询的"下一次取用下标"需要在多进程/多 worker 间共享，进程内全局变量无法满足，
因此持久化到数据库。游标键由 {profile_id}:{usage}:{priority} 组成。
"""

from sqlmodel import Field, SQLModel


class ChannelCursor(SQLModel, table=True):
    """渠道加权轮询游标表"""

    __tablename__ = "channel_cursor"

    # 形如 "{profile_id}:{usage}:{priority}"，唯一标识一个优先级组的轮询游标
    cursor_key: str = Field(primary_key=True, max_length=255)
    # 下一次取用的展开序列下标
    position: int = Field(default=0, nullable=False)
