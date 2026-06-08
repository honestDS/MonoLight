from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"
SQLITE_DB_PATH = DATA_DIR / "monolight.db"
CHROMA_DB_PATH = DATA_DIR / "chromadb"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
