import asyncio
import json
import logging
import os
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Token
from pathlib import Path

from loguru import logger

from app.core.constants import LOG_MESSAGE_MAX_LENGTH, MSG_LOG_MESSAGE_TRUNCATED
from app.core.i18n import t
from app.core.i18n.context import reset_current_log_locale, set_current_log_locale
from app.core.i18n.locale import DEFAULT_LOCALE
from app.core.paths import DEFAULT_LOG_FILE_PATH, TOOLS_LOG_FILENAME
from app.core.utils.time import get_local_time


def get_profile_log_locale(profile: object | None) -> str:
    return DEFAULT_LOCALE


def set_system_log_locale(locale: str | None) -> Token[str | None]:
    return set_current_log_locale(locale or DEFAULT_LOCALE)


def reset_system_log_locale(token: Token[str | None]) -> None:
    reset_current_log_locale(token)


def set_profile_log_locale(profile: object | None) -> Token[str | None]:
    return set_current_log_locale(get_profile_log_locale(profile))


def reset_profile_log_locale(token: Token[str | None]) -> None:
    reset_current_log_locale(token)


@contextmanager
def profile_log_locale(profile: object | None) -> Iterator[None]:
    token = set_current_log_locale(get_profile_log_locale(profile))
    try:
        yield
    finally:
        reset_current_log_locale(token)


def build_process_log_path(log_path: str, process_id: int | None = None) -> str:
    path = Path(log_path)
    pid = process_id if process_id is not None else os.getpid()
    return str(path.with_name(f"{path.stem}.{pid}{path.suffix}"))


class LogManager:
    _configured = False

    @staticmethod
    def _truncate_tool_log_message(record: dict) -> str:
        """对工具类日志的超大 message 做字符级截断。

        用于数据库 sink，避免超大工具结果撑大数据库存储体积与前端广播 payload；
        文件 sink 直接使用 record["message"] 原文写入，不受此截断影响，保留完整数据用于审计。
        """
        message = record["message"]
        is_tool_log = "tool_call" in record["extra"] or "tool_result" in record["extra"]
        if is_tool_log and message and len(message) > LOG_MESSAGE_MAX_LENGTH:
            message = message[:LOG_MESSAGE_MAX_LENGTH] + t(MSG_LOG_MESSAGE_TRUNCATED, original_length=len(message))
        return message

    @classmethod
    def setup(cls, log_path: str = str(DEFAULT_LOG_FILE_PATH), level: str = "INFO"):
        if cls._configured:
            return

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
                extra_data = {}
                for extra_key, extra_value in record["extra"].items():
                    if extra_key not in ["name", "uid", "session_id"]:
                        extra_data[extra_key] = extra_value
                extra_json = json.dumps(extra_data, default=str) if extra_data else None

                # 优先使用 extra 中的 name 作为 module
                module_name = record["extra"].get("name") or record["name"]

                # 仅对工具类日志（tool_call / tool_result）的超大 message 做截断，
                # 避免工具结果撑大数据库存储体积
                db_log_message = cls._truncate_tool_log_message(record)

                log_entry = SystemLogCreate(
                    level=record["level"].name,
                    module=module_name,
                    message=db_log_message,
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

        process_log_path = build_process_log_path(abs_log_path)

        # 每个进程写入独立文件，避免 Windows 下多个进程同时轮转同一文件时发生占用冲突。
        logger.add(
            process_log_path,
            level=level,
            rotation="10 MB",
            retention="1 week",
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            format="[{time:YYYY-MM-DD HH:mm:ss.SSS}] [{level}] [{file}:{line}]: {message}",
        )

        # 添加专用工具日志
        tool_log_path = build_process_log_path(str(Path(abs_log_path).parent / TOOLS_LOG_FILENAME))
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

        # 数据库同时承担跨进程实时日志传输，因此需记录 DEBUG 及以上级别。
        # 此处必须 enqueue=False，否则会在线程中因缺少事件循环导致异步任务丢失。
        logger.add(
            db_sink_wrapper,
            level="DEBUG",
            enqueue=False,
        )

        # WebSocket 由各 Web Worker 轮询数据库日志后广播，确保跨进程日志可见且不会重复推送。

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
        lines = []
        for raw_line in command.splitlines():
            line = raw_line.strip()
            if line:
                lines.append(line)
        log_cmd = lines if len(lines) > 1 else command.strip()
        logger.bind(tool_call=True, session_id=session_id, uid=uid).info(t("LOG_TOOL_CALL", turn=turn, tool_name=tool_name, args=log_cmd))

    @staticmethod
    def log_tool_result(turn: int, result: str, session_id: str = "default", uid: str = None):
        # 记录工具执行结果日志
        logger.bind(tool_result=True, session_id=session_id, uid=uid).info(t("LOG_TOOL_RESULT", turn=turn, result=result))


def get_logger(name: str):
    return logger.bind(name=name)


def channel_log_extra(channel, model_entry: dict) -> dict:
    """构造渠道相关日志扩展信息：渠道名、模型名等"""
    model_id = model_entry["model_id"]
    channel_name = getattr(channel, "name", None)
    return {
        "channel_id": channel.id,
        "channel_name": f"{channel_name} / {model_id}",
        "model_id": model_id,
        "model_name": model_id,
    }
