from .job_claim import CRUDLongTermMemoryMutationJobClaim
from .job_common import (
    MemoryJobCancelResult as MemoryJobCancelResult,
)
from .job_control import CRUDLongTermMemoryMutationJobControl
from .job_query import CRUDLongTermMemoryMutationJobQuery
from .job_terminal import CRUDLongTermMemoryMutationJobTerminal

__all__ = [
    "MemoryJobCancelResult",
    "CRUDLongTermMemoryMutationJob",
    "memory_job_crud",
    "long_term_memory_mutation_job_crud",
]


class CRUDLongTermMemoryMutationJob(
    CRUDLongTermMemoryMutationJobQuery,
    CRUDLongTermMemoryMutationJobClaim,
    CRUDLongTermMemoryMutationJobTerminal,
    CRUDLongTermMemoryMutationJobControl,
):
    pass


memory_job_crud = CRUDLongTermMemoryMutationJob()
long_term_memory_mutation_job_crud = memory_job_crud
