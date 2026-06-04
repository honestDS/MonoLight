import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.core.utils.dt import get_local_time


class LogManager:
    _configured = False

    @classmethod
    def setup(cls, log_path: str = "data/logs/monolight.log", level: str = "INFO"):
        if cls._configured:
            return

        # 设置时区补丁
        def patch_record(record):
            # 直接使用工具函数获取带时区的当前时间并替换记录时间
            # record["time"] 是 loguru 生成的，包含微秒，astimezone 会保留精度
            record["time"] = get_local_time()

        logger.configure(patcher=patch_record)

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

                # 序列化 extra
                extra_data = {k: v for k, v in record["extra"].items() if k not in ["uid", "session_id"]}
                extra_json = json.dumps(extra_data) if extra_data else None

                log_entry = SystemLogCreate(
                    level=record["level"].name,
                    module=record["name"],
                    message=record["message"],
                    uid=uid,
                    session_id=session_id,
                    extra=extra_json,
                    created_at=record["time"] # record["time"] 已经是 patch 过的带时区 datetime
                )

                async with AsyncSessionLocal() as db:
                    await system_log_crud.create(db, obj_in=log_entry)
                    await db.commit()
            except Exception as e:
                # 避免循环日志
                sys.stderr.write(f"Error in DB log sink: {str(e)}\n")

        # 封装异步函数供 loguru 使用
        def sink_wrapper(message):
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    loop.create_task(db_sink(message))
                else:
                    # 如果没有运行中的 loop，尝试使用新 loop 运行
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        new_loop.run_until_complete(db_sink(message))
                    finally:
                        new_loop.close()
            except Exception as e:
                sys.stderr.write(f"Critical error in log sink wrapper: {str(e)}\n")

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

        # 添加数据库 Sink (仅记录 INFO 及以上级别)
        logger.add(
            sink_wrapper,
            level="INFO",
            enqueue=True, # 确保线程安全
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
        # 记录启动时的信息，方便排查
        now_aware = get_local_time()
        logger.info(
            f"Log system initialized. Path: {abs_log_path} | "
            f"Time: {now_aware.isoformat()}"
        )

    @staticmethod
    def log_tool_call(turn: int, tool_name: str, command: str, session_id: str = "default", uid: str = None):
        # 记录工具调用日志
        lines = [line.strip() for line in command.splitlines() if line.strip()]
        log_cmd = lines if len(lines) > 1 else command.strip()
        logger.bind(tool_call=True, session_id=session_id, uid=uid).info(
            f"Turn {turn} | Tool: {tool_name} | Args: {log_cmd}"
        )

    @staticmethod
    def log_tool_result(turn: int, result: str, session_id: str = "default", uid: str = None):
        # 记录工具执行结果日志
        logger.bind(tool_result=True, session_id=session_id, uid=uid).info(
            f"Turn {turn} | Result: {result}"
        )


def get_logger(name: str):
    return logger.bind(name=name)
