from .store_record_lifecycle import CRUDLongTermMemoryRecordLifecycle
from .store_record_mutation import CRUDLongTermMemoryRecordMutation
from .store_record_query import CRUDLongTermMemoryRecordQuery

__all__ = [
    "CRUDLongTermMemoryRecord",
]


class CRUDLongTermMemoryRecord(
    CRUDLongTermMemoryRecordQuery,
    CRUDLongTermMemoryRecordMutation,
    CRUDLongTermMemoryRecordLifecycle,
):
    pass
