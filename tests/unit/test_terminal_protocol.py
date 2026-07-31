import itertools

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalActionReceipt,
    TerminalCloseRequest,
    TerminalOutputBufferState,
    TerminalOutputReadStatus,
    TerminalPermissionScope,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalRequest,
    TerminalResizeRequest,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
    TerminalStatusRequest,
    TerminalWriteRequest,
    can_transition_terminal_status,
    generate_terminal_request_id,
    generate_terminal_session_id,
    validate_terminal_status_transition,
)

TERMINAL_SESSION_ID = "t" * 32
REQUEST_ID = "r" * 16
REQUEST_ADAPTER = TypeAdapter(TerminalRequest)

EXPECTED_TRANSITIONS = {
    TerminalSessionStatus.STARTING: {
        TerminalSessionStatus.RUNNING,
        TerminalSessionStatus.CLOSING,
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    },
    TerminalSessionStatus.RUNNING: {
        TerminalSessionStatus.CLOSING,
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    },
    TerminalSessionStatus.CLOSING: {
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    },
    TerminalSessionStatus.EXITED: set(),
    TerminalSessionStatus.FAILED: set(),
    TerminalSessionStatus.LOST: set(),
}


def _permission_scope(**overrides) -> TerminalPermissionScope:
    payload = {
        "owner_uid": "user-1",
        "owner_session_id": "session-1",
        "original_tool_call_id": "tool-call-1",
        "audit_record_id": 1,
        "audit_execution_record_id": 1,
    }
    payload.update(overrides)
    return TerminalPermissionScope(**payload)


def _output_buffer(**overrides) -> TerminalOutputBufferState:
    payload = {
        "capacity_bytes": 1024,
        "oldest_offset": 0,
        "next_offset": 0,
        "oldest_sequence": 1,
        "next_sequence": 1,
    }
    payload.update(overrides)
    return TerminalOutputBufferState(**payload)


@pytest.mark.parametrize(
    ("current", "target"),
    list(itertools.product(TerminalSessionStatus, repeat=2)),
)
def test_terminal_status_transition_matrix(
    current: TerminalSessionStatus,
    target: TerminalSessionStatus,
):
    expected = current == target or target in EXPECTED_TRANSITIONS[current]

    assert can_transition_terminal_status(current, target) is expected
    if expected:
        assert validate_terminal_status_transition(current, target) is target
    else:
        with pytest.raises(ValueError):
            validate_terminal_status_transition(current, target)


def test_terminal_final_statuses_are_immutable_and_idempotent():
    assert isinstance(TERMINAL_SESSION_FINAL_STATUSES, frozenset)
    assert TERMINAL_SESSION_FINAL_STATUSES == {
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    }
    for status in TERMINAL_SESSION_FINAL_STATUSES:
        assert can_transition_terminal_status(status, status)


def test_fast_exit_allows_starting_to_exited_snapshot():
    assert (
        validate_terminal_status_transition(
            TerminalSessionStatus.STARTING,
            TerminalSessionStatus.EXITED,
        )
        is TerminalSessionStatus.EXITED
    )
    snapshot = TerminalSessionSnapshot(
        terminal_session_id=TERMINAL_SESSION_ID,
        status=TerminalSessionStatus.EXITED,
        permission_scope=_permission_scope(),
        output_buffer=_output_buffer(),
        exit_code=0,
    )

    assert snapshot.exit_code == 0
    assert snapshot.failure_reason is None


@pytest.mark.parametrize(
    ("status", "exit_code", "failure_reason"),
    [
        (TerminalSessionStatus.EXITED, None, None),
        (TerminalSessionStatus.EXITED, 0, "driver error"),
        (TerminalSessionStatus.FAILED, None, None),
        (TerminalSessionStatus.FAILED, 1, "driver error"),
        (TerminalSessionStatus.FAILED, None, "   "),
        (TerminalSessionStatus.LOST, None, None),
        (TerminalSessionStatus.LOST, 1, "worker ownership lost"),
        (TerminalSessionStatus.LOST, None, "   "),
        (TerminalSessionStatus.STARTING, 0, None),
        (TerminalSessionStatus.RUNNING, None, "driver error"),
        (TerminalSessionStatus.CLOSING, 0, "driver error"),
    ],
)
def test_terminal_snapshot_rejects_invalid_outcome_fields(
    status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
):
    with pytest.raises(ValidationError):
        TerminalSessionSnapshot(
            terminal_session_id=TERMINAL_SESSION_ID,
            status=status,
            permission_scope=_permission_scope(),
            output_buffer=_output_buffer(),
            exit_code=exit_code,
            failure_reason=failure_reason,
        )


@pytest.mark.parametrize(
    ("status", "exit_code", "failure_reason"),
    [
        (TerminalSessionStatus.FAILED, None, "driver error"),
        (TerminalSessionStatus.LOST, None, "worker ownership lost"),
        (TerminalSessionStatus.STARTING, None, None),
        (TerminalSessionStatus.RUNNING, None, None),
        (TerminalSessionStatus.CLOSING, None, None),
    ],
)
def test_terminal_snapshot_accepts_status_specific_outcomes(
    status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
):
    snapshot = TerminalSessionSnapshot(
        terminal_session_id=TERMINAL_SESSION_ID,
        status=status,
        permission_scope=_permission_scope(),
        output_buffer=_output_buffer(),
        exit_code=exit_code,
        failure_reason=failure_reason,
    )

    assert snapshot.status is status


def test_permission_scope_requires_exact_owner_session_and_authorized_action():
    scope = _permission_scope(allowed_actions=frozenset({TerminalAction.READ, TerminalAction.WRITE}))

    assert scope.permits("user-1", "session-1", TerminalAction.READ)
    assert scope.permits("user-1", "session-1", TerminalAction.WRITE)
    assert not scope.permits("user-2", "session-1", TerminalAction.READ)
    assert not scope.permits("user-1", "session-2", TerminalAction.READ)
    assert _permission_scope().allowed_actions == ALL_TERMINAL_ACTIONS


def test_permission_scope_rejects_empty_allowed_actions():
    with pytest.raises(ValidationError):
        _permission_scope(allowed_actions=frozenset())


def test_generated_terminal_identifiers_are_unique_and_valid():
    session_ids = {generate_terminal_session_id() for _ in range(10)}
    request_ids = {generate_terminal_request_id() for _ in range(10)}

    assert len(session_ids) == 10
    assert len(request_ids) == 10
    for session_id in session_ids:
        request = TerminalStatusRequest(terminal_session_id=session_id)
        assert request.terminal_session_id == session_id
    for request_id in request_ids:
        request = TerminalWriteRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            request_id=request_id,
            data="x",
        )
        assert request.request_id == request_id


@pytest.mark.parametrize(
    ("payload", "request_type"),
    [
        (
            {"action": "status", "terminal_session_id": TERMINAL_SESSION_ID},
            TerminalStatusRequest,
        ),
        (
            {"action": "read", "terminal_session_id": TERMINAL_SESSION_ID},
            TerminalReadRequest,
        ),
        (
            {
                "action": "write",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "request_id": REQUEST_ID,
                "data": "ls",
            },
            TerminalWriteRequest,
        ),
        (
            {
                "action": "resize",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "request_id": REQUEST_ID,
                "columns": 80,
                "rows": 24,
            },
            TerminalResizeRequest,
        ),
        (
            {
                "action": "close",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "request_id": REQUEST_ID,
            },
            TerminalCloseRequest,
        ),
    ],
)
def test_terminal_request_discriminator_selects_action_model(
    payload: dict,
    request_type: type,
):
    request = REQUEST_ADAPTER.validate_python(payload)

    assert isinstance(request, request_type)


def test_terminal_requests_forbid_extra_fields_and_unknown_actions():
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(
            {
                "action": "status",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "request_id": REQUEST_ID,
            }
        )
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python({"action": "unknown", "terminal_session_id": TERMINAL_SESSION_ID})
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(
            {
                "action": "signal",
                "terminal_session_id": TERMINAL_SESSION_ID,
                "request_id": REQUEST_ID,
                "signal": "interrupt",
            }
        )


@pytest.mark.parametrize(
    "terminal_session_id",
    ["t" * 31, "t" * 129, "invalid/session"],
)
def test_terminal_session_id_boundaries_are_enforced(terminal_session_id: str):
    with pytest.raises(ValidationError):
        TerminalStatusRequest(terminal_session_id=terminal_session_id)


@pytest.mark.parametrize(
    "request_id",
    ["r" * 15, "r" * 129, "invalid/request"],
)
def test_mutating_request_id_boundaries_are_enforced(request_id: str):
    with pytest.raises(ValidationError):
        TerminalWriteRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            request_id=request_id,
            data="x",
        )


def test_read_write_and_resize_boundaries_are_enforced():
    assert TerminalReadRequest(terminal_session_id=TERMINAL_SESSION_ID).max_bytes == 65_536
    assert (
        TerminalReadRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            offset=0,
            max_bytes=1_048_576,
        ).offset
        == 0
    )
    assert TerminalWriteRequest(
        terminal_session_id=TERMINAL_SESSION_ID,
        request_id=REQUEST_ID,
        data="x" * 65_536,
    ).data
    assert (
        TerminalResizeRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            request_id=REQUEST_ID,
            columns=1_000,
            rows=1_000,
        ).columns
        == 1_000
    )

    for payload in [
        {"terminal_session_id": TERMINAL_SESSION_ID, "offset": -1},
        {"terminal_session_id": TERMINAL_SESSION_ID, "max_bytes": 0},
        {"terminal_session_id": TERMINAL_SESSION_ID, "max_bytes": 1_048_577},
    ]:
        with pytest.raises(ValidationError):
            TerminalReadRequest(**payload)
    for data in ["", "x" * 65_537]:
        with pytest.raises(ValidationError):
            TerminalWriteRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                request_id=REQUEST_ID,
                data=data,
            )
    for columns, rows in [(0, 1), (1, 0), (1_001, 1), (1, 1_001)]:
        with pytest.raises(ValidationError):
            TerminalResizeRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                request_id=REQUEST_ID,
                columns=columns,
                rows=rows,
            )


def test_output_buffer_state_requires_monotonic_offsets_and_sequences():
    state = _output_buffer(
        capacity_bytes=4,
        oldest_offset=4,
        next_offset=8,
        oldest_sequence=2,
        next_sequence=5,
    )

    assert state.next_offset == 8
    assert state.next_sequence == 5
    for overrides in [
        {"capacity_bytes": 0},
        {"oldest_offset": 8, "next_offset": 7},
        {"oldest_sequence": 3, "next_sequence": 2},
        {"capacity_bytes": 3, "next_offset": 4, "next_sequence": 2},
        {"next_sequence": 2},
        {"next_offset": 1, "next_sequence": 1},
    ]:
        with pytest.raises(ValidationError):
            _output_buffer(**overrides)


@pytest.mark.parametrize(
    "result",
    [
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.OK,
            requested_offset=4,
            start_offset=4,
            next_offset=7,
            oldest_available_offset=2,
            latest_offset=10,
            sequence=3,
            output="abc",
            eof=False,
        ),
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.EMPTY,
            requested_offset=10,
            start_offset=10,
            next_offset=10,
            oldest_available_offset=2,
            latest_offset=10,
            sequence=4,
            output="",
            eof=False,
        ),
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.TRUNCATED,
            requested_offset=1,
            start_offset=2,
            next_offset=6,
            oldest_available_offset=2,
            latest_offset=10,
            sequence=5,
            output="data",
            eof=False,
        ),
        TerminalReadResult(
            terminal_session_id=TERMINAL_SESSION_ID,
            read_status=TerminalOutputReadStatus.EXPIRED,
            requested_offset=4,
            start_offset=10,
            next_offset=10,
            oldest_available_offset=10,
            latest_offset=10,
            sequence=0,
            output="",
            eof=True,
        ),
    ],
)
def test_read_result_accepts_each_protocol_status(result: TerminalReadResult):
    assert isinstance(result.read_status, TerminalOutputReadStatus)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "read_status": TerminalOutputReadStatus.OK,
            "requested_offset": 10,
            "start_offset": 10,
            "next_offset": 10,
        },
        {"read_status": TerminalOutputReadStatus.OK, "output": ""},
        {"read_status": TerminalOutputReadStatus.OK, "next_offset": 4},
        {"read_status": TerminalOutputReadStatus.OK, "sequence": 0},
        {
            "read_status": TerminalOutputReadStatus.TRUNCATED,
            "requested_offset": 2,
            "start_offset": 2,
        },
        {
            "read_status": TerminalOutputReadStatus.TRUNCATED,
            "requested_offset": 1,
            "start_offset": 2,
            "output": "",
        },
        {
            "read_status": TerminalOutputReadStatus.TRUNCATED,
            "requested_offset": 1,
            "start_offset": 2,
            "next_offset": 2,
        },
        {
            "read_status": TerminalOutputReadStatus.TRUNCATED,
            "requested_offset": 1,
            "start_offset": 2,
            "sequence": 0,
        },
        {
            "read_status": TerminalOutputReadStatus.EMPTY,
            "requested_offset": 9,
            "start_offset": 10,
        },
        {"read_status": TerminalOutputReadStatus.EMPTY, "output": "unexpected"},
        {
            "read_status": TerminalOutputReadStatus.EXPIRED,
            "oldest_available_offset": 2,
            "start_offset": 10,
            "next_offset": 10,
            "output": "",
            "eof": True,
        },
        {
            "read_status": TerminalOutputReadStatus.EXPIRED,
            "oldest_available_offset": 10,
            "start_offset": 10,
            "next_offset": 10,
            "output": "retained",
            "eof": True,
        },
        {
            "read_status": TerminalOutputReadStatus.EXPIRED,
            "oldest_available_offset": 10,
            "start_offset": 10,
            "next_offset": 10,
            "output": "",
            "eof": False,
        },
        {"requested_offset": 11},
        {"start_offset": 7, "next_offset": 6},
    ],
)
def test_read_result_rejects_invalid_status_combinations(overrides: dict):
    payload = {
        "terminal_session_id": TERMINAL_SESSION_ID,
        "read_status": TerminalOutputReadStatus.OK,
        "requested_offset": 4,
        "start_offset": 4,
        "next_offset": 6,
        "oldest_available_offset": 2,
        "latest_offset": 10,
        "sequence": 1,
        "output": "data",
        "eof": False,
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        TerminalReadResult(**payload)


def test_action_receipt_serializes_duplicate_flag_and_mutating_action():
    receipt = TerminalActionReceipt(
        terminal_session_id=TERMINAL_SESSION_ID,
        request_id=REQUEST_ID,
        action=TerminalAction.WRITE,
        duplicate=True,
        session_status=TerminalSessionStatus.RUNNING,
    )

    assert receipt.model_dump(mode="json") == {
        "terminal_session_id": TERMINAL_SESSION_ID,
        "request_id": REQUEST_ID,
        "action": "write",
        "duplicate": True,
        "session_status": "running",
    }
