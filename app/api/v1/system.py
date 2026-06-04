
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import check_admin_privilege
from app.core.crud.log import system_log_crud
from app.models.system_log import SystemLogResponse
from app.providers.database import get_db
from app.schemas.response import StandardResponse

router = APIRouter(prefix="/system", tags=["System Monitoring"], dependencies=[Depends(check_admin_privilege)])

@router.get("/logs", response_model=StandardResponse)
async def get_system_logs(
    level: str | None = Query(None, description="日志级别"),
    uid: str | None = Query(None, description="用户UID"),
    session_id: str | None = Query(None, description="会话ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    """
    分页获取系统日志，支持按级别、用户或会话过滤。
    """
    logs = await system_log_crud.get_multi_filtered(
        db,
        level=level,
        uid=uid,
        session_id=session_id,
        skip=skip,
        limit=limit
    )

    data = [SystemLogResponse.model_validate(log) for log in logs]
    return StandardResponse.success(data=data, message="日志获取成功")
