import os
import shutil
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, FileResponse

from app.core.security import get_current_user

router = APIRouter()

TEMP_DIR = os.path.join(os.getcwd(), "temp")

@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """
    文件上传接口。
    前端可以将文件或图片上传到此接口，服务端保存并返回其在服务器上的本地路径。
    文件会以 `temp/temp_{session_id}/文件名` 的结构隔离保存。
    """
    if not session_id:
        session_id = f"unassigned_{uuid.uuid4().hex[:8]}"

    session_dir = os.path.join(TEMP_DIR, f"temp_{session_id}")
    os.makedirs(session_dir, exist_ok=True)

    # 构造安全且唯一的文件名以避免覆盖
    safe_filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file_path = os.path.join(session_dir, safe_filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")
    finally:
        file.file.close()

    # 返回支持跨平台兼容的统一格式绝对路径
    absolute_path = os.path.abspath(file_path)

    return {"path": absolute_path, "filename": file.filename, "session_id": session_id}

@router.get("/download")
async def download_file(path: str):
    """
    文件下载/预览接口。
    根据绝对路径返回文件内容。
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    filename = os.path.basename(path)
    # 去除 uuid 前缀以显示原始文件名 (8位uuid + _)
    display_name = filename[9:] if len(filename) > 9 and filename[8] == '_' else filename
    return FileResponse(path, filename=display_name)
