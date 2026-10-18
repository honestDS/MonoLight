import json
from pathlib import Path
from typing import Any

import pytest

import app.core.audit.service as audit_service_module
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.constants import AUDIT_TERMINAL_CONTEXT_MAX_CHARS, AUDIT_TERMINAL_HISTORY_LIMIT
from app.core.crud.audit.audit import audit_crud
from app.core.crud.terminal.session import terminal_control_command_crud, terminal_session_crud
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.models.audit import AuditRecordStatus, AuditToolConclusion
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig
from app.models.terminal_session import (
    TerminalControlCommand,
    TerminalControlCommandStatus,
    TerminalSession,
)

AUDIT_MODEL_ID = "terminal-context-audit-model"
UID = "terminal-context-user"
SESSION_ID = "terminal-context-chat-session"
TERMINAL_SESSION_ID = "terminal-audit-context-session-0001"


@pytest.fixture
async def audit_database(database_factory):
    async with database_factory(name="terminal-audit-context.sqlite3") as session_factory:
        yield session_factory


def _audit_config(tmp_path: Path, *, threshold: int = 0) -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": 1,
                            "model_id": AUDIT_MODEL_ID,
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                }
            },
            "security": {
                "audit_channel_id": 1,
                "audit_model_id": AUDIT_MODEL_ID,
                "audit_threshold": threshold,
                "audit_report_language": "zh",
                "protect_system_directories": True,
            },
            "tool": {
                "enabled_tools": ["terminal_write"],
                "allowed_operation_dirs": [str(tmp_path)],
            },
            "other": {},
            "memory": {"enabled": False},
        }
    )


def _audit_channel() -> ModelChannel:
    return ModelChannel(
        id=1,
        name="terminal-context-audit-channel",
        api_key="enc:v1:test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {
                "model_id": AUDIT_MODEL_ID,
                "usage": "CHAT",
                "protocol": "OPENAI",
                "context_window_k": 32,
                "max_tokens": 512,
                "temperature": 0,
                "top_p": 1,
            }
        ],
    )


async def _seed_audit_channel(session_factory) -> None:
    async with session_factory() as db:
        db.add(_audit_channel())
        await db.commit()


def _patch_audit_root(monkeypatch: pytest.MonkeyPatch, audit_root: Path) -> None:
    async def persist_in_test_root(db, **kwargs: Any) -> bool:
        return await persist_prepared_audit_round(db, audit_root=audit_root, **kwargs)

    monkeypatch.setattr(audit_service_module, "persist_prepared_audit_round", persist_in_test_root)


def _audit_response(
    tool_call_ids: list[str],
    *,
    score: int = 5,
    deny: bool = False,
) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=json.dumps(
                {
                    "results": [
                        {
                            "tool_call_id": tool_call_id,
                            "score": score,
                            "reason": "terminal context integration test",
                            "deny": deny,
                            "file_checks": [],
                        }
                        for tool_call_id in tool_call_ids
                    ]
                }
            ),
        ),
        model=AUDIT_MODEL_ID,
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        finish_reason="stop",
    )


def _text_response(content: str) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
        model=AUDIT_MODEL_ID,
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        finish_reason="stop",
    )


async def _audit_round(
    db,
    *,
    cfg: ProfileConfig,
    tool_call: InternalToolCall,
    working_directory: Path,
    uid: str = UID,
    session_id: str = SESSION_ID,
):
    return await audit_service_module.audit_tool_round(
        db,
        cfg=cfg,
        tool_calls=[tool_call],
        source_assistant_message_id=101,
        uid=uid,
        operator_username="tester",
        session_id=session_id,
        source="integration",
        language="zh",
        working_directory=working_directory,
    )


def _terminal_control(
    request_id: str,
    *,
    action: TerminalAction = TerminalAction.WRITE,
    payload: dict[str, Any],
    status: TerminalControlCommandStatus = TerminalControlCommandStatus.SUCCEEDED,
    result: dict[str, Any] | None = None,
) -> TerminalControlCommand:
    if result is None and status is TerminalControlCommandStatus.SUCCEEDED and action is TerminalAction.WRITE:
        result = {"bytes_written": len(str(payload.get("data", "")))}
    return TerminalControlCommand(
        terminal_session_id=TERMINAL_SESSION_ID,
        request_id=request_id,
        action=action,
        payload=payload,
        payload_hash="0" * 64,
        status=status,
        result=result,
    )


async def _seed_terminal_session(
    session_factory,
    *,
    command: str = "cmd.exe /Q",
    uid: str = UID,
    session_id: str = SESSION_ID,
    controls: list[TerminalControlCommand] | None = None,
) -> None:
    async with session_factory() as db:
        db.add(
            TerminalSession(
                terminal_session_id=TERMINAL_SESSION_ID,
                uid=uid,
                session_id=session_id,
                original_tool_call_id="initial-shell-call",
                profile_id=1,
                command=command,
                working_directory="/workspace/initial-terminal-directory",
                status=TerminalSessionStatus.RUNNING,
                allowed_actions=[action.value for action in TerminalAction],
            )
        )
        db.add_all(controls or [])
        await db.commit()


@pytest.mark.asyncio
async def test_get_audit_context_bounds_recent_terminal_history(audit_database) -> None:
    long_command = "launch " + "c" * (AUDIT_TERMINAL_CONTEXT_MAX_CHARS // 2)
    controls = [_terminal_control(f"discarded-{index}", payload={"data": f"discarded-{index}"}) for index in range(4)]
    controls.extend(
        _terminal_control(
            f"write-{index:02}",
            payload={"data": f"write-{index:02}:" + "d" * 512},
        )
        for index in range(17)
    )
    controls.extend(
        [
            _terminal_control(
                "pending-write",
                payload={"data": "pending:" + "p" * 512},
                status=TerminalControlCommandStatus.PENDING,
            ),
            _terminal_control(
                "failed-write",
                payload={"data": "failed:" + "f" * 512},
                status=TerminalControlCommandStatus.FAILED,
            ),
            _terminal_control(
                "latest-read",
                action=TerminalAction.READ,
                payload={"offset": 0, "max_bytes": 65_536},
                result={
                    "read_status": "ok",
                    "output": "o" * 5_000,
                    "output_truncated": True,
                },
            ),
        ]
    )
    await _seed_terminal_session(audit_database, command=long_command, controls=controls)

    async with audit_database() as db:
        context = await terminal_session_manager.get_audit_context(
            db,
            TERMINAL_SESSION_ID,
            UID,
            SESSION_ID,
        )
        all_controls = await terminal_control_command_crud.list_by_session(db, TERMINAL_SESSION_ID)

    history = context["history"]
    assert context["initial_working_directory"] == "/workspace/initial-terminal-directory"
    assert context["command"] == long_command[: AUDIT_TERMINAL_CONTEXT_MAX_CHARS // 4]
    assert context["command_truncated"] is True
    assert context["status"] == TerminalSessionStatus.RUNNING.value
    assert context["history_truncated"] is True
    assert context["output_is_live"] is False
    assert len(all_controls) > AUDIT_TERMINAL_HISTORY_LIMIT
    assert len(history) == AUDIT_TERMINAL_HISTORY_LIMIT
    assert [item["id"] for item in history] == [item.id for item in all_controls[-AUDIT_TERMINAL_HISTORY_LIMIT:]]
    assert [item["id"] for item in history] == sorted(item["id"] for item in history)

    controls_by_id = {item.id: item for item in all_controls}
    write_history = [item for item in history if item["action"] == TerminalAction.WRITE.value]
    assert len(write_history) == 19
    assert [item["status"] for item in write_history[-2:]] == [
        TerminalControlCommandStatus.PENDING.value,
        TerminalControlCommandStatus.FAILED.value,
    ]
    assert all(isinstance(item["data_truncated"], bool) for item in write_history)
    assert any(not item["data_truncated"] for item in write_history)
    assert any(item["data_truncated"] for item in write_history)
    for item in write_history:
        original_data = controls_by_id[item["id"]].payload["data"]
        assert item["data_truncated"] is (len(item["data"]) < len(original_data))
        if item["data_truncated"]:
            if item["data"]:
                assert item["data"] == original_data[-len(item["data"]) :]
        else:
            assert item["data"] == original_data
        if item["status"] == TerminalControlCommandStatus.SUCCEEDED.value:
            assert item["bytes_written"] == len(original_data)
        else:
            assert item["status"] in {
                TerminalControlCommandStatus.PENDING.value,
                TerminalControlCommandStatus.FAILED.value,
            }
            assert "bytes_written" not in item

    read_history = [item for item in history if item["action"] == TerminalAction.READ.value]
    assert len(read_history) == 1
    assert read_history[0]["output"] == "o" * 5_000
    assert read_history[0]["output_truncated"] is True
    assert len(context["command"]) + sum(len(item.get(field, "")) for item in history for field in ("data", "output") if isinstance(item.get(field), str)) <= AUDIT_TERMINAL_CONTEXT_MAX_CHARS


@pytest.mark.asyncio
async def test_terminal_write_scoring_and_summary_share_read_only_context(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")
    await _seed_terminal_session(
        audit_database,
        controls=[_terminal_control("existing-write", payload={"data": "echo prior\n"})],
    )
    original_data = "echo should wait for confirmation\n"
    tool_call = InternalToolCall(
        id="pending-terminal-write",
        name="terminal_write",
        arguments={"terminal_session_id": TERMINAL_SESSION_ID, "data": original_data},
    )
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            return _audit_response([tool_call.id], score=5, deny=False)
        return _text_response("描述性摘要：这项操作看起来风险较低。")

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, threshold=5),
            tool_call=tool_call,
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert result.confirmation_payload is not None
    assert len(generate_calls) == 2
    assert generate_calls[0]["messages"][0].content == AUDIT_BATCH_PROMPT
    assert generate_calls[1]["messages"][0].content == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh")

    scoring_payload = json.loads(generate_calls[0]["messages"][1].content)
    summary_payload = json.loads(generate_calls[1]["messages"][1].content)
    scoring_call = scoring_payload["tool_calls"][0]
    summary_call = summary_payload["tool_calls"][0]
    terminal_context = scoring_call["terminal_context"]
    assert terminal_context == summary_call["terminal_context"]
    assert [item["data"] for item in terminal_context["history"]] == ["echo prior\n"]
    assert scoring_call["arguments"]["data"] == original_data
    assert summary_call["arguments"]["data"] == original_data
    assert tool_call.arguments["data"] == original_data

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        controls = await terminal_control_command_crud.list_by_session(db, TERMINAL_SESSION_ID)
        terminal_session = await terminal_session_crud.get(db, TERMINAL_SESSION_ID)

    assert record is not None and record.status is AuditRecordStatus.PENDING
    assert len(details) == 1
    assert details[0].conclusion is AuditToolConclusion.PENDING
    assert details[0].score == 5
    assert len(controls) == 1
    assert terminal_session is not None and terminal_session.status is TerminalSessionStatus.RUNNING


@pytest.mark.parametrize(
    ("caller_uid", "caller_session_id"),
    [
        ("external-terminal-user", SESSION_ID),
        (UID, "external-terminal-chat-session"),
    ],
    ids=["external-user", "external-chat-session"],
)
@pytest.mark.asyncio
async def test_external_terminal_session_context_fails_before_model_call(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caller_uid: str,
    caller_session_id: str,
) -> None:
    await _seed_audit_channel(audit_database)
    _patch_audit_root(monkeypatch, tmp_path / "audit")
    await _seed_terminal_session(
        audit_database,
        controls=[_terminal_control("existing-write", payload={"data": "private terminal history"})],
    )
    tool_call = InternalToolCall(
        id="external-terminal-write",
        name="terminal_write",
        arguments={"terminal_session_id": TERMINAL_SESSION_ID, "data": "echo private evidence"},
    )
    generate_calls: list[dict[str, Any]] = []

    async def unexpected_generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        raise AssertionError("an external terminal context must not reach the audit model")

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", unexpected_generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path),
            tool_call=tool_call,
            working_directory=tmp_path,
            uid=caller_uid,
            session_id=caller_session_id,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert generate_calls == []

    async with audit_database() as db:
        controls = await terminal_control_command_crud.list_by_session(db, TERMINAL_SESSION_ID)
        terminal_session = await terminal_session_crud.get(db, TERMINAL_SESSION_ID)

    assert len(controls) == 1
    assert terminal_session is not None and terminal_session.status is TerminalSessionStatus.RUNNING
