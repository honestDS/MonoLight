from app.core.log import get_logger
from app.core.terminal.schemas import (
    TerminalCloseRequest,
    TerminalResizeRequest,
    TerminalWriteRequest,
)

__all__ = [
    "TERMINAL_SESSION_LEASE_SECONDS",
    "TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS",
    "TERMINAL_SESSION_POLL_INTERVAL_SECONDS",
    "TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS",
    "TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS",
    "TerminalMutatingRequest",
]

logger = get_logger(__name__)

TERMINAL_SESSION_LEASE_SECONDS = 60

TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS = 20

TERMINAL_SESSION_POLL_INTERVAL_SECONDS = 0.2

TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS = 1.0

TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS = 1.0

type TerminalMutatingRequest = TerminalWriteRequest | TerminalResizeRequest | TerminalCloseRequest

_TERMINAL_MUTATING_REQUEST_TYPES = (
    TerminalWriteRequest,
    TerminalResizeRequest,
    TerminalCloseRequest,
)
