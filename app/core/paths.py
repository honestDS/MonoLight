import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.constants import ERR_AUDIT_RECORD_ID_INVALID, ERR_AUDIT_USER_DIR_INVALID, ERR_AUDIT_USER_ID_INVALID
from app.core.i18n import t

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = ROOT_DIR / "dashboard"
DASHBOARD_PUBLIC_DIR = DASHBOARD_DIR / "public"
FAVICON_PATH = DASHBOARD_PUBLIC_DIR / "favicon.ico"

DATA_DIR = ROOT_DIR / "data"
SYSTEM_SECRETS_PATH = DATA_DIR / "system_secrets.json"
SYSTEM_SECRETS_LOCK_PATH = DATA_DIR / "system_secrets.lock"
LOGS_DIR = DATA_DIR / "logs"
AUDIT_DIR = DATA_DIR / "audit"
TEMP_DIR = ROOT_DIR / "temp"

SQLITE_DB_PATH = DATA_DIR / "monolight.db"
CHROMA_DB_PATH = DATA_DIR / "chromadb"
DEFAULT_LOG_FILE_PATH = LOGS_DIR / "monolight.log"
TOOLS_LOG_FILENAME = "tools.log"
USER_TEMP_DIR_PREFIX = "temp_"
AUDIT_FILE_PREFIX = "audit_"
AUDIT_FILE_SUFFIX = ".json"
TEST_SESSION_DB_PATH = Path(tempfile.gettempdir()) / "monolight_test_session.db"


def get_user_temp_dir(project_root: str | Path, uid: str) -> Path:
    return Path(project_root) / TEMP_DIR.name / f"{USER_TEMP_DIR_PREFIX}{uid}"


def get_user_audit_dir(uid: str, *, audit_root: str | Path = AUDIT_DIR) -> Path:
    if not uid or uid in {".", ".."} or PurePosixPath(uid).name != uid or PureWindowsPath(uid).name != uid:
        raise ValueError(t(ERR_AUDIT_USER_ID_INVALID))
    root_path = Path(audit_root).resolve(strict=False)
    user_path = root_path / f"{USER_TEMP_DIR_PREFIX}{uid}"
    if user_path.parent != root_path or user_path.name != f"{USER_TEMP_DIR_PREFIX}{uid}":
        raise ValueError(t(ERR_AUDIT_USER_DIR_INVALID))
    return user_path


def get_audit_file_path(uid: str, audit_record_id: int, *, audit_root: str | Path = AUDIT_DIR) -> Path:
    if audit_record_id < 1:
        raise ValueError(t(ERR_AUDIT_RECORD_ID_INVALID))
    return get_user_audit_dir(uid, audit_root=audit_root) / f"{AUDIT_FILE_PREFIX}{audit_record_id}{AUDIT_FILE_SUFFIX}"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
