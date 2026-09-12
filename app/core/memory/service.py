from .service_create import LongTermMemoryCreate
from .service_delete import LongTermMemoryDelete
from .service_embedding import append_memory_embedding_delta as append_memory_embedding_delta
from .service_pin import LongTermMemoryPin
from .service_recall import LongTermMemoryRecall
from .service_resume import LongTermMemoryResume
from .service_update import LongTermMemoryUpdate


class LongTermMemoryService(
    LongTermMemoryCreate,
    LongTermMemoryUpdate,
    LongTermMemoryDelete,
    LongTermMemoryResume,
    LongTermMemoryPin,
    LongTermMemoryRecall,
):
    pass


memory_service = LongTermMemoryService()

__all__ = [
    "LongTermMemoryService",
    "append_memory_embedding_delta",
    "memory_service",
]
