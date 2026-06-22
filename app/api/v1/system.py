import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import check_admin_privilege
from app.core import constants
from app.core.crud.log import system_log_crud
from app.core.i18n.context import set_current_locale
from app.core.i18n.locale import normalize_locale
from app.core.log import get_logger
from app.core.log_broadcaster import log_broadcaster
from app.models.system_log import SystemLogResponse
from app.providers.database import get_db
from app.schemas.response import (
    PageData,
    StandardResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/system", tags=["System Monitoring"], dependencies=[Depends(check_admin_privilege)])


@router.get("/logs", response_model=StandardResponse)
async def get_system_logs(
    level: str | None = Query(None, description="日志级别"),
    uid: str | None = Query(None, description="用户UID"),
    session_id: str | None = Query(None, description="会话ID"),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """
    分页获取系统日志，支持按级别、用户或会话过滤。
    """
    skip = (page - 1) * size
    logs = await system_log_crud.get_multi_filtered(db, level=level, uid=uid, session_id=session_id, skip=skip, limit=size)
    total = await system_log_crud.count_filtered(db, level=level, uid=uid, session_id=session_id)

    page_data = PageData(
        items=[SystemLogResponse.model_validate(log) for log in logs],
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(data=page_data, message=constants.MSG_LOG_LIST_SUCCESS)


@router.websocket("/logs/ws")
async def system_logs_websocket(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    系统日志实时监控 WebSocket 接口。
    连接后首先下发最近 100 条历史日志，随后进入实时推送模式。
    """
    # 语言上下文需在 accept 之前设置；WebSocket 的 accept 统一由 log_broadcaster.connect 内部完成，此处不再重复 accept
    lang_param = websocket.query_params.get("lang")
    set_current_locale(normalize_locale(lang_param if lang_param is not None else ""))
    await log_broadcaster.connect(websocket)
    try:
        # 1. 回溯历史日志 (最近 100 条)
        history_logs = await system_log_crud.get_multi_filtered(db, limit=100)
        # 注意：CRUD 返回的是按时间倒序的，发送前需反转
        history_logs.reverse()

        # 历史日志集中一次性推送，使用 type=history 标识批量消息，避免逐条发送的网络开销
        history_payload = []
        for log in history_logs:
            extra_str = log.extra or ""
            try:
                extra_dict = json.loads(extra_str) if extra_str else {}
            except json.JSONDecodeError:
                extra_dict = {}
            history_payload.append(
                {
                    "timestamp": log.created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "level": log.level,
                    "module": log.module,
                    "message": log.message,
                    "uid": log.uid,
                    "session_id": log.session_id,
                    "extra": extra_dict,
                }
            )
        await websocket.send_json({"type": "history", "logs": history_payload})

        # 2. 保持连接，等待广播器推送（保持心跳或等待断开）
        while True:
            # 维持连接活跃，检测断开
            await websocket.receive_text()

    except WebSocketDisconnect:
        await log_broadcaster.disconnect(websocket)
    except Exception as e:
        logger.error(f"System logs WS error: {e}")
        await log_broadcaster.disconnect(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
