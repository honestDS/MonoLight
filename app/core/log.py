import logging
import os
import sys
from pathlib import Path

from loguru import logger


class LogManager:
    _configured = False

    @classmethod
    def setup(cls, log_path: str = "data/logs/monolight.log", level: str = "INFO"):
        if cls._configured:
            return

        # 确保工作目录
        os.getcwd()
        if not os.path.isabs(log_path):
            abs_log_path = str(Path(log_path).resolve())
        else:
            abs_log_path = log_path

        # 处理目录不存在的情况
        Path(abs_log_path).parent.mkdir(parents=True, exist_ok=True)

        # 移除默认处理器
        logger.remove()

        # 添加控制台输出 (带颜色)
        logger.add(
            sys.stdout,
            level=level,
            colorize=True,
            format=(
                "<green>[{time:YYYY-MM-DD HH:mm:ss.SSS}]</green> "
                "<level>[{level}]</level> <cyan>[{file}:{line}]</cyan>: <level>{message}</level>"
            ),
        )

        # 添加文件输出 (自动滚动)
        logger.add(
            abs_log_path,
            level=level,
            rotation="10 MB",
            retention="1 week",
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            format="[{time:YYYY-MM-DD HH:mm:ss.SSS}] [{level}] [{file}:{line}]: {message}",
        )

        # 添加专用工具日志
        tool_log_path = str(Path(abs_log_path).parent / "tools.log")
        logger.add(
            tool_log_path,
            filter=lambda record: "tool_call" in record["extra"] or "tool_result" in record["extra"],
            level="DEBUG",
            rotation="10 MB",
            retention="1 week",
            encoding="utf-8",
            enqueue=True,
            format="[{time:YYYY-MM-DD HH:mm:ss.SSS}] [{level}] {message}",
        )

        # 拦截标准 logging
        class InterceptHandler(logging.Handler):
            def emit(self, record):
                try:
                    level = logger.level(record.levelname).name
                except ValueError:
                    level = record.levelno

                frame, depth = logging.currentframe(), 2
                while (
                    frame is not None and frame.f_code.co_filename == logging.__file__
                ):
                    frame = frame.f_back
                    depth += 1

                logger.opt(depth=depth, exception=record.exc_info).log(
                    level, record.getMessage()
                )

        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

        cls._configured = True
        logger.info(f"Log system initialized. Path: {abs_log_path}")

    @staticmethod
    def log_tool_call(turn: int, tool_name: str, command: str, session_id: str = "default"):
        # 记录工具调用日志
        lines = [line.strip() for line in command.splitlines() if line.strip()]
        log_cmd = lines if len(lines) > 1 else command.strip()
        logger.bind(tool_call=True).info(
            f"Session: {session_id} | Turn {turn} | Tool: {tool_name} | Args: {log_cmd}"
        )

    @staticmethod
    def log_tool_result(turn: int, result: str, session_id: str = "default"):
        # 记录工具执行结果日志
        logger.bind(tool_result=True).info(
            f"Session: {session_id} | Turn {turn} | Result: {result}"
        )


def get_logger(name: str):
    return logger.bind(name=name)
