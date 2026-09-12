from .store_history import (
    CRUDLongTermMemoryEmbeddingDelta,
    CRUDLongTermMemoryEmbeddingRevision,
    CRUDLongTermMemoryReference,
    CRUDLongTermMemoryRevision,
)
from .store_record import CRUDLongTermMemoryRecord
from .store_state import CRUDLongTermMemoryEmbeddingSelectionToken, CRUDLongTermMemoryStore

__all__ = [
    "memory_store_crud",
    "memory_embedding_selection_token_crud",
    "memory_record_crud",
    "memory_revision_crud",
    "memory_embedding_revision_crud",
    "memory_embedding_delta_crud",
    "memory_reference_crud",
    "long_term_memory_store_crud",
    "long_term_memory_embedding_selection_token_crud",
    "long_term_memory_record_crud",
    "long_term_memory_revision_crud",
    "long_term_memory_embedding_revision_crud",
    "long_term_memory_embedding_delta_crud",
    "long_term_memory_reference_crud",
]

memory_store_crud = CRUDLongTermMemoryStore()
memory_embedding_selection_token_crud = CRUDLongTermMemoryEmbeddingSelectionToken()
memory_record_crud = CRUDLongTermMemoryRecord()
memory_revision_crud = CRUDLongTermMemoryRevision()
memory_embedding_revision_crud = CRUDLongTermMemoryEmbeddingRevision()
memory_embedding_delta_crud = CRUDLongTermMemoryEmbeddingDelta()
memory_reference_crud = CRUDLongTermMemoryReference()

long_term_memory_store_crud = memory_store_crud
long_term_memory_embedding_selection_token_crud = memory_embedding_selection_token_crud
long_term_memory_record_crud = memory_record_crud
long_term_memory_revision_crud = memory_revision_crud
long_term_memory_embedding_revision_crud = memory_embedding_revision_crud
long_term_memory_embedding_delta_crud = memory_embedding_delta_crud
long_term_memory_reference_crud = memory_reference_crud
