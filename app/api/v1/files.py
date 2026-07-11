import os
import shutil
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.exceptions import ForbiddenException, ResourceNotFoundException, ServerException
from app.core.paths import TEMP_DIR, get_user_temp_dir
from app.core.security import get_current_user
from app.core.tools.send_file_to_user import resolve_file_token
from app.core.utils.session import ensure_web_session_writable
from app.providers.database import get_db

router = APIRouter()


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if session_id:
        await ensure_web_session_writable(
            db,
            session_id=session_id,
            uid=getattr(current_user, "uid", None),
        )
    else:
        session_id = f"unassigned_{uuid.uuid4().hex[:8]}"

    session_dir = get_user_temp_dir(TEMP_DIR.parent, session_id)
    os.makedirs(session_dir, exist_ok=True)
    safe_filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file_path = os.path.join(session_dir, safe_filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as exc:
        raise ServerException(message=constants.ERR_INTERNAL_SERVER_ERROR, cause=str(exc)) from exc
    finally:
        file.file.close()

    absolute_path = os.path.abspath(file_path)
    return {"path": absolute_path, "filename": file.filename, "session_id": session_id}


@router.get("/download")
async def download_file(path: str):
    if not os.path.exists(path):
        raise ResourceNotFoundException(message=constants.ERR_KB_DOC_NOT_FOUND)
    filename = os.path.basename(path)
    display_name = filename[9:] if len(filename) > 9 and filename[8] == "_" else filename
    return FileResponse(path, filename=display_name)


@router.get("/download-sent")
async def download_sent_file(token: str):
    try:
        path = resolve_file_token(token)
    except FileNotFoundError as exc:
        raise ResourceNotFoundException(message=constants.ERR_KB_DOC_NOT_FOUND) from exc
    except Exception as exc:
        raise ForbiddenException(message=constants.ERR_UNAUTHORIZED) from exc

    return FileResponse(path, filename=path.name)
