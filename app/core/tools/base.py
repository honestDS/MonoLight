import abc
import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from app.core.log import get_logger


class BaseExecutor(abc.ABC):
    def __init__(self, project_root: str, uid: str = "default"):
        self.project_root = Path(project_root)
        self.uid = uid
        self.logger = get_logger(self.__class__.__name__)
        self.cfg = None
        self.db = None
        self.profile = None
        self.session_id = None
        self.allowed_knowledge_base_ids = []

    async def run_sync(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """使用 asyncio.to_thread 执行同步函数，由系统原生管理线程池并随调度器信号量动态控制并发"""
        p_func = partial(func, *args, **kwargs)
        return await asyncio.to_thread(p_func)

    def set_config(self, cfg):
        self.cfg = cfg

    def set_runtime_context(
        self,
        db=None,
        profile=None,
        session_id: str | None = None,
        allowed_knowledge_base_ids: list[int] | None = None,
    ):
        self.db = db
        self.profile = profile
        self.session_id = session_id
        self.allowed_knowledge_base_ids = allowed_knowledge_base_ids or []

    @abc.abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑并返回字符串结果"""
        pass
