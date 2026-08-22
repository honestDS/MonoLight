import pytest
from sqlalchemy.dialects import mysql, sqlite

from app.core.crud.audit import build_audit_status_update, build_passed_execution_claim_update, build_pending_execution_claim_update
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecordStatus


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_all_audit_conditional_writes_compile_without_dialect_specific_syntax(dialect):
    now = get_local_time()
    statements = [
        build_audit_status_update(1, AuditRecordStatus.PREPARING, status=AuditRecordStatus.PENDING, updated_at=now),
        build_pending_execution_claim_update(
            audit_record_id=1,
            uid="u1",
            session_id="s1",
            now=now,
            claim_token="pending-token",
            decision_message_id=2,
            decision_raw_message="approve",
            decided_by="tester",
        ),
        build_passed_execution_claim_update(
            audit_record_id=1,
            now=now,
            claim_token="passed-token",
        ),
    ]

    for statement in statements:
        compiled = str(statement.compile(dialect=dialect)).upper()
        assert compiled.startswith("UPDATE AUDIT_RECORD")
        assert "WHERE" in compiled
        assert "RETURNING" not in compiled
        assert "ON CONFLICT" not in compiled
        assert "INSERT IGNORE" not in compiled
