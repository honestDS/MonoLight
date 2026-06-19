import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from loguru import logger

from app.core.paths import DEFAULT_LOG_FILE_PATH, TOOLS_LOG_FILENAME
from app.core.utils.time import get_local_time


class LogManager:
    _configured = False

    @classmethod
    def setup(cls, log_path: str = str(DEFAULT_LOG_FILE_PATH), level: str = "INFO"):
        if cls._configured:
            return

        # 异步 WebSocket 推送器
        async def ws_sink(message):
            try:
                from app.core.log_broadcaster import log_broadcaster

                record = message.record
                uid = record["extra"].get("uid")
                session_id = record["extra"].get("session_id")

                # 序列化 extra 时使用 default=str 避免非基本类型序列化失败
                # 排除 name, uid, session_id，因为它们已经有专门的字段
                extra_data = {k: v for k, v in record["extra"].items() if k not in ["name", "uid", "session_id"]}

                # 使用系统本地时间戳推送给前端
                local_now = get_local_time()
                # 优先使用 extra 中的 name 作为 module
                module_name = record["extra"].get("name") or record["name"]
                log_entry = {
                    "timestamp": local_now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "level": record["level"].name,
                    "module": module_name,
                    "message": record["message"],
                    "uid": uid,
                    "session_id": session_id,
                    "extra": extra_data,
                }
                await log_broadcaster.broadcast(log_entry)
            except Exception as e:
                sys.stderr.write(f"Error in WS log sink: {str(e)}\n")

        # 异步数据库写入器
        async def db_sink(message):
            try:
                from app.core.crud.log import system_log_crud
                from app.models.system_log import SystemLogCreate
                from app.providers.database import AsyncSessionLocal

                record = message.record
                # 提取 extra 中的关键字段
                uid = record["extra"].get("uid")
                session_id = record["extra"].get("session_id")

                # 序列化 extra 时使用 default=str 避免非基本类型序列化失败
                # 排除 name, uid, session_id，因为它们已经有专门的列
                extra_data = {k: v for k, v in record["extra"].items() if k not in ["name", "uid", "session_id"]}
                extra_json = json.dumps(extra_data, default=str) if extra_data else None

                # 优先使用 extra 中的 name 作为 module
                module_name = record["extra"].get("name") or record["name"]

                log_entry = SystemLogCreate(
                    level=record["level"].name,
                    module=module_name,
                    message=record["message"],
                    uid=uid,
                    session_id=session_id,
                    extra=extra_json,
                    created_at=get_local_time(),  # 显式使用包含时区的本地时间写入数据库
                )

                # 使用全新的 session 处理，规避并发下的 session 冲突与关闭异常
                async with AsyncSessionLocal() as db:
                    await system_log_crud.create(db, obj_in=log_entry)
            except Exception as e:
                # 避免循环日志并打印完整堆栈异常
                import traceback

                sys.stderr.write(f"Error in DB log sink: {str(e)}\n")
                traceback.print_exc(file=sys.stderr)

        # 封装异步函数供 loguru 使用 (DB)
        def db_sink_wrapper(message):
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    loop.create_task(db_sink(message))
            except Exception as e:
                sys.stderr.write(f"Critical error in DB log sink wrapper: {str(e)}\n")

        # 封装异步函数供 loguru 使用 (WS)
        def ws_sink_wrapper(message):
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    loop.create_task(ws_sink(message))
            except Exception as e:
                sys.stderr.write(f"Critical error in WS log sink wrapper: {str(e)}\n")

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
            format=("<green>[{time:YYYY-MM-DD HH:mm:ss.SSS}]</green> <level>[{level}]</level> <cyan>[{file}:{line}]</cyan>: <level>{message}</level>"),
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
        tool_log_path = str(Path(abs_log_path).parent / TOOLS_LOG_FILENAME)
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

        # 添加数据库 Sink (仅记录 INFO 及以上级别)
        # 注意：此处必须 enqueue=False，否则会在无事件循环 of 线程运行导致异步任务丢失
        logger.add(
            db_sink_wrapper,
            level="INFO",
            enqueue=False,
        )

        # 添加 WebSocket Sink (记录全量级别以支持前端实时调试)
        logger.add(
            ws_sink_wrapper,
            level="DEBUG",
            enqueue=False,
        )

        # 拦截标准 logging
        class InterceptHandler(logging.Handler):
            def emit(self, record):
                try:
                    level = logger.level(record.levelname).name
                except ValueError:
                    level = record.levelno

                frame, depth = logging.currentframe(), 2
                while frame is not None and frame.f_code.co_filename == logging.__file__:
                    frame = frame.f_back
                    depth += 1

                logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

        cls._configured = True

    @staticmethod
    def log_tool_call(turn: int, tool_name: str, command: str, session_id: str = "default", uid: str = None):
        # 记录工具调用日志
        lines = [line.strip() for line in command.splitlines() if line.strip()]
        log_cmd = lines if len(lines) > 1 else command.strip()
        logger.bind(tool_call=True, session_id=session_id, uid=uid).info(f"Turn {turn} | Tool: {tool_name} | Args: {log_cmd}")

    @staticmethod
    def log_tool_result(turn: int, result: str, session_id: str = "default", uid: str = None):
        # 记录工具执行结果日志
        logger.bind(tool_result=True, session_id=session_id, uid=uid).info(f"Turn {turn} | Result: {result}")


def get_logger(name: str):
    return logger.bind(name=name)


def channel_log_extra(provider, model_entry: dict) -> dict:
    """构造渠道相关日志扩展信息：渠道名、模型名等"""
    model_id = model_entry["model_id"]
    provider_name = getattr(provider, "name", None)
    return {
        "provider_id": provider.id,
        "provider_name": provider_name,
        "model_id": model_id,
        "model_name": model_id,
        "channel_name": f"{provider_name} / {model_id}",
    }
