import os
from datetime import datetime, timedelta, timezone


def get_local_time() -> datetime:
    """
    根据环境变量 LOG_TZ_OFFSET 获取当前带时区的本地时间。
    默认为北京时间 (UTC+8)。
    """
    try:
        offset = int(os.getenv("LOG_TZ_OFFSET", "8"))
    except (ValueError, TypeError):
        offset = 8
    
    tz = timezone(timedelta(hours=offset))
    return datetime.now(tz)
