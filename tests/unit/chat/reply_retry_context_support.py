import json
from types import SimpleNamespace

import pytest

from app.core.constants import GOAL_EXECUTION_PHASE_RUNNING, TOOL_WORKFLOW_FOREGROUND
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import interactive_runtime as interactive_runtime_module
from app.core.dispatchers import non_stream as non_stream_module
from app.core.dispatchers import stream as stream_module
from app.models.message import InternalMessage, MessageRole
from app.models.profile import ToolConfig


class _Session:
    def __init__(self):
        self.commit_count = 0
        self.info = {}

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, session):
        return None


class _DatabaseLikeSession(_Session):
    async def execute(self, *args, **kwargs):
        return None


class _Dispatcher(non_stream_module.NonStreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(
        cls,
        db,
        message,
        uid,
        session_id,
        profile,
        attachments,
        *,
        tool_workflow=None,
    ):
        return None


class _StreamDispatcher(stream_module.StreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(
        cls,
        db,
        message,
        uid,
        session_id,
        profile,
        attachments,
        *,
        tool_workflow=None,
    ):
        return None


@pytest.fixture(autouse=True)
def _patch_session_crud(monkeypatch):
    async def get_session_by_id(*args, **kwargs):
        return SimpleNamespace(
            goal_mode=False,
            source="http",
            max_turns=5,
            context_summary_revision=0,
            context_content_revision=0,
            llm_request_metadata=None,
            stream_llm_request=None,
        )

    monkeypatch.setattr(interactive_runtime_module.session_crud, "get_by_session_id", get_session_by_id)


async def _passthrough_context_summary_checkpoint(db, **kwargs):
    return kwargs["messages"]


def _build_runtime_snapshot(max_tokens, platform_constraints=None):
    snapshot = {"response_settings": {"max_output_tokens": max_tokens}}
    if platform_constraints:
        snapshot["platform_constraints"] = platform_constraints
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


async def _build_max_tokens_runtime_instruction(
    _db,
    _session_id,
    max_tokens,
    *,
    platform_constraints=None,
):
    return _build_runtime_snapshot(max_tokens, platform_constraints=platform_constraints)


class _Channel:
    id = 1
    name = "test-channel"
    usage_key = "0123456789abcdef0123456789abcdef"
    base_url = "https://example.invalid"

    def get_decrypted_api_key(self):
        return "secret"


class _Logger:
    def __init__(self):
        self.info_messages = []

    def bind(self, **kwargs):
        return self

    def info(self, message):
        self.info_messages.append(message)

    def warning(self, message):
        return None

    def error(self, message, **kwargs):
        return None


def _context_length_generation_state(*, stream_event_callback=None):
    return SimpleNamespace(
        db=_Session(),
        uid="user-1",
        session_id="session-1",
        goal_mode=False,
        tool_workflow=TOOL_WORKFLOW_FOREGROUND,
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(tool=ToolConfig(), other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=[InternalMessage(id=1, role=MessageRole.USER, content="request")],
        checkpoint_state=interactive_helpers_module._ExecutionCheckpointState(
            callback=None,
            turn_messages=[],
            files_to_user=[],
            upper_message_id=1,
            total_output_tokens=0,
            session_total_input_tokens=0,
            session_total_cached_tokens=0,
            session_total_output_tokens=0,
            execution_phase=GOAL_EXECUTION_PHASE_RUNNING,
        ),
        context_summary_work_validity_checker=None,
        context_summary_lifecycle_callback=None,
        context_frozen=False,
        chat_params={
            "temperature": None,
            "top_p": None,
            "max_tokens": 512,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
        model_entry={"model_id": "model-1", "usage": "CHAT", "protocol": "OPENAI"},
        chat_channel=object(),
        chat_cursor_key="1:CHAT",
        chat_channel_obj=_Channel(),
        channel_rule=SimpleNamespace(priority=1),
        latest_llm_request_metadata=None,
        stream_llm_request=None,
        current_turn=1,
        stream_event_callback=stream_event_callback,
        request_metadata_callback=None,
        expose_tool_call_content=True,
        show_tool_calls=True,
        dispatcher_mode="stream" if stream_event_callback is not None else "non_stream",
        dispatch_logger=_Logger(),
        img_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
