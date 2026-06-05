import platform

import psutil

from app.core.utils.dt import get_local_time


def get_system_info() -> str:
    """
    获取当前运行系统的关键元数据。
    """
    try:
        uname = platform.uname()
        cpu_usage = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()

        info = [
            f"OS: {uname.system} {uname.release} ({uname.machine})",
            f"Architecture: {platform.architecture()[0]}",
            f"Python Version: {platform.python_version()}",
            f"CPU Usage: {cpu_usage}%",
            f"Memory Usage: {memory.percent}% ({memory.used // (1024**2)}MB / {memory.total // (1024**2)}MB)",
        ]
        return " | ".join(info)
    except Exception as e:
        return f"System info unavailable: {e}"


def get_formatted_system_time() -> str:
    """
    获取格式化的本地当前时间字符串。
    """
    now = get_local_time()
    # %A=星期几
    # %Z=时区
    return now.strftime("%Y-%m-%d %H:%M:%S %A %Z")


def get_full_system_context() -> str:
    """
    聚合系统信息与时间，生成供 Agent 参考的上下文字符串。
    """
    return f"Current System Context: [Time: {get_formatted_system_time()}] [System: {get_system_info()}]"
