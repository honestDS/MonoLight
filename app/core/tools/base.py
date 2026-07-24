import abc
import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from app.core.dispatch_context import DispatchContext
from app.core.log import get_logger


class BaseExecutor(abc.ABC):
    requires_audit: ClassVar[bool]

    def __init__(self, project_root: str, uid: str = "default"):
        self.project_root = Path(project_root)
        self.uid = uid
        self.logger = get_logger(self.__class__.__name__)
        self.cfg = None
        self.db = None
        self.profile = None
        self.session_id = None
        self.allowed_knowledge_base_ids = []
        self.dispatch_context: DispatchContext | None = None

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
        dispatch_context: DispatchContext | None = None,
    ):
        self.dispatch_context = dispatch_context
        self.db = dispatch_context.db if dispatch_context else db
        self.profile = dispatch_context.profile if dispatch_context else profile
        self.session_id = dispatch_context.session_id if dispatch_context else session_id
        self.allowed_knowledge_base_ids = dispatch_context.allowed_knowledge_base_ids if dispatch_context else (allowed_knowledge_base_ids or [])

    @abc.abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑并返回字符串结果"""
        pass
