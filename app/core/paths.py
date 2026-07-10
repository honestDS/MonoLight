import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = ROOT_DIR / "dashboard"
DASHBOARD_PUBLIC_DIR = DASHBOARD_DIR / "public"
FAVICON_PATH = DASHBOARD_PUBLIC_DIR / "favicon.ico"

DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
TEMP_DIR = ROOT_DIR / "temp"

SQLITE_DB_PATH = DATA_DIR / "monolight.db"
CHROMA_DB_PATH = DATA_DIR / "chromadb"
DEFAULT_LOG_FILE_PATH = LOGS_DIR / "monolight.log"
TOOLS_LOG_FILENAME = "tools.log"
USER_TEMP_DIR_PREFIX = "temp_"
TEST_SESSION_DB_PATH = Path(tempfile.gettempdir()) / "monolight_test_session.db"


def get_user_temp_dir(project_root: str | Path, uid: str) -> Path:
    return Path(project_root) / TEMP_DIR.name / f"{USER_TEMP_DIR_PREFIX}{uid}"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
