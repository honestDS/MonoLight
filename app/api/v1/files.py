import asyncio
import os
import shutil
import uuid
from pathlib import PurePosixPath, PureWindowsPath

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_FILE_ARGUMENT_INVALID,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_KB_DOC_NOT_FOUND,
    ERR_SESSION_NO_PERMISSION,
    ERR_UNAUTHORIZED,
)
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException, ServerException
from app.core.paths import TEMP_DIR, get_user_temp_dir
from app.core.security import get_current_user
from app.core.tools.send_file_to_user import resolve_file_token
from app.core.utils.file_access import resolve_user_temp_file
from app.core.utils.session import ensure_web_session_writable, get_session_for_user
from app.models.user import User
from app.providers.database import get_db

router = APIRouter()


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        owner_uid = current_user.uid
        if session_id:
            session = await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=current_user.uid,
                is_admin=getattr(current_user, "is_superuser", False),
            )
            if session is not None:
                owner_uid = session.uid
        else:
            session_id = f"unassigned_{uuid.uuid4().hex[:8]}"

        filename = file.filename
        if not isinstance(filename, str) or not filename or "\x00" in filename or filename in {".", ".."} or PurePosixPath(filename).name != filename or PureWindowsPath(filename).name != filename or PurePosixPath(filename).is_absolute() or PureWindowsPath(filename).is_absolute() or PureWindowsPath(filename).drive:
            raise ParameterException(ERR_FILE_ARGUMENT_INVALID)

        if (
            not isinstance(owner_uid, str)
            or not owner_uid
            or "\x00" in owner_uid
            or owner_uid in {".", ".."}
            or PurePosixPath(owner_uid).name != owner_uid
            or PureWindowsPath(owner_uid).name != owner_uid
            or PurePosixPath(owner_uid).is_absolute()
            or PureWindowsPath(owner_uid).is_absolute()
            or PureWindowsPath(owner_uid).drive
        ):
            raise ParameterException(ERR_FILE_ARGUMENT_INVALID)

        user_dir = get_user_temp_dir(TEMP_DIR.parent, owner_uid)
        expected_dir_name = f"temp_{owner_uid}"
        normalized_temp_dir = TEMP_DIR.resolve(strict=False)
        normalized_user_dir = user_dir.resolve(strict=False)
        if normalized_user_dir.parent != normalized_temp_dir or normalized_user_dir.name != expected_dir_name:
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

        os.makedirs(user_dir, exist_ok=True)
        normalized_user_dir = user_dir.resolve(strict=False)
        if normalized_user_dir.parent != normalized_temp_dir or normalized_user_dir.name != expected_dir_name:
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

        safe_filename = f"{uuid.uuid4().hex[:8]}_{filename}"
        file_path = os.path.join(user_dir, safe_filename)

        def save_file() -> None:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

        await asyncio.to_thread(save_file)
    except (ParameterException, ForbiddenException):
        raise
    except Exception as exc:
        raise ServerException(message=ERR_INTERNAL_SERVER_ERROR, cause=str(exc)) from exc
    finally:
        await file.close()

    absolute_path = os.path.abspath(file_path)
    return {"path": absolute_path, "filename": filename, "session_id": session_id}


@router.get("/download")
async def download_file(
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_id: str | None = None,
):
    owner_uid = current_user.uid
    if session_id:
        session = await get_session_for_user(
            db,
            session_id=session_id,
            uid=current_user.uid,
            is_admin=current_user.is_superuser,
        )
        owner_uid = session.uid

    resolved_path = await resolve_user_temp_file(path, uid=owner_uid, db=db, temp_root=TEMP_DIR)
    filename = resolved_path.name
    display_name = filename[9:] if len(filename) > 9 and filename[8] == "_" else filename
    return FileResponse(resolved_path, filename=display_name)


@router.get("/download-sent")
async def download_sent_file(token: str, current_user: User = Depends(get_current_user)):
    try:
        path = await asyncio.to_thread(
            resolve_file_token,
            token,
            uid=current_user.uid,
            is_admin=bool(current_user.is_superuser),
        )
    except FileNotFoundError as exc:
        raise ResourceNotFoundException(message=ERR_KB_DOC_NOT_FOUND) from exc
    except Exception as exc:
        raise ForbiddenException(message=ERR_UNAUTHORIZED) from exc

    return FileResponse(path, filename=path.name)
