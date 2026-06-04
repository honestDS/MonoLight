import abc
from pathlib import Path

from app.core.log import get_logger


class BaseExecutor(abc.ABC):
    def __init__(self, project_root: str, uid: str = "default"):
        self.project_root = Path(project_root)
        self.uid = uid
        self.logger = get_logger(self.__class__.__name__)

    @abc.abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑并返回字符串结果"""
        pass
