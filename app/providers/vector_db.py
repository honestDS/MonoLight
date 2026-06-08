import chromadb
from chromadb.config import Settings

from app.core.paths import CHROMA_DB_PATH, ensure_data_dirs

ensure_data_dirs()

chroma_client = chromadb.PersistentClient(
    path=str(CHROMA_DB_PATH),
    settings=Settings(anonymized_telemetry=False),
)


def get_chroma_client():
    return chroma_client


def create_collection(collection_name: str):
    return chroma_client.create_collection(name=collection_name)


def get_or_create_collection(collection_name: str):
    return chroma_client.get_or_create_collection(name=collection_name)


def get_collection(collection_name: str):
    return chroma_client.get_collection(name=collection_name)


def delete_collection(collection_name: str):
    chroma_client.delete_collection(name=collection_name)


def delete_collection_items(collection_name: str, ids: list[str]):
    if not ids:
        return
    collection = get_collection(collection_name)
    collection.delete(ids=ids)
