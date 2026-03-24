import abc
from pathlib import Path
from typing import Any

class BaseExecutor(abc.ABC):
    def __init__(self, project_root: str, uid: str = "default"):
        self.project_root = Path(project_root)
        self.uid = uid

    @abc.abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑并返回字符串结果"""
        pass
