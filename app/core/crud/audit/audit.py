from .claims import CRUDAuditClaims
from .execution import CRUDAuditExecution
from .preparation import CRUDAuditPreparation
from .recovery import CRUDAuditRecovery

__all__ = [
    "CRUDAudit",
    "audit_crud",
]


class CRUDAudit(
    CRUDAuditPreparation,
    CRUDAuditClaims,
    CRUDAuditExecution,
    CRUDAuditRecovery,
):
    pass


audit_crud = CRUDAudit()
