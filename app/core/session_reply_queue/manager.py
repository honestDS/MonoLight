from .manager_common import (
    build_identified_work_response as build_identified_work_response,
)
from .manager_common import (
    build_input_queued_event as build_input_queued_event,
)
from .manager_common import (
    build_session_reply_work_event_id as build_session_reply_work_event_id,
)
from .manager_common import (
    build_session_reply_work_identity as build_session_reply_work_identity,
)
from .manager_common import (
    get_work_request_ids as get_work_request_ids,
)
from .manager_common import (
    is_submission_queued as is_submission_queued,
)
from .manager_enqueue import SessionReplyEnqueue
from .manager_freeze import SessionReplyFreeze
from .manager_result import SessionReplyResult
from .manager_submission import SessionReplySubmission

__all__ = [
    "build_identified_work_response",
    "build_input_queued_event",
    "build_session_reply_work_event_id",
    "build_session_reply_work_identity",
    "get_work_request_ids",
    "is_submission_queued",
    "SessionReplyQueueManager",
    "session_reply_queue_manager",
]


class SessionReplyQueueManager(
    SessionReplySubmission,
    SessionReplyEnqueue,
    SessionReplyFreeze,
    SessionReplyResult,
):
    pass


session_reply_queue_manager = SessionReplyQueueManager()
