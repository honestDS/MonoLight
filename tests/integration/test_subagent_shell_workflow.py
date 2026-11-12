import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import psutil
import pytest
from sqlalchemy import select

import app.core.background_tasks.runner as runner_module
from app.core.background_tasks.terminal_watch_coordinator import complete_terminal_watch_round
from app.core.background_tasks.terminal_watch_reply import route_terminal_auto_watch_reply
from app.core.constants import AGENT_WORK_TOOL_NAME, ERR_SUBAGENT_CANCELLED, ERR_TERMINAL_COMMAND_FAILED, ERR_TOOL_COMMAND_TIMEOUT, SHELL_TOOL_NAME, TERMINAL_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.shell import ShellExecutor
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.models.terminal_session import TerminalSession


async def _child_for_parent(db, parent_work_id: int) -> SessionReplyWorkItem:
    return (
        (
            await db.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                    SessionReplyWorkItem.source_id == str(parent_work_id),
                )
            )
        )
        .scalars()
        .one()
    )


async def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        await asyncio.sleep(0.05)


def _process_is_running(pid: int, create_time: float | None) -> bool:
    try:
        process = psutil.Process(pid)
        if create_time is not None and process.create_time() != create_time:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.AccessDenied:
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


async def _wait_for_process_exit(pid: int, create_time: float | None, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_is_running(pid, create_time):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for process {pid} to exit")
        await asyncio.sleep(0.05)


def _python_command(*arguments: Path) -> str:
    argv = [sys.executable, *(str(argument) for argument in arguments)]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _read_process_identities(path: Path) -> dict[str, tuple[int, float]]:
    payload = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(payload, dict):
        raise ValueError("process identity marker must contain an object")
    identities: dict[str, tuple[int, float]] = {}
    for name in ("script", "descendant"):
        identity = payload.get(name)
        if not isinstance(identity, dict):
            raise ValueError(f"missing process identity for {name}")
        pid = identity.get("pid")
        create_time = identity.get("create_time")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError(f"invalid pid for {name}")
        if not isinstance(create_time, (int, float)) or isinstance(create_time, bool):
            raise ValueError(f"invalid create time for {name}")
        identities[name] = (pid, float(create_time))
    return identities


def _kill_process_identities(path: Path) -> None:
    try:
        identities = _read_process_identities(path)
    except (OSError, TypeError, ValueError, UnicodeError):
        return

    for name in ("descendant", "script"):
        pid, create_time = identities[name]
        if pid == os.getpid():
            continue
        try:
            process = psutil.Process(pid)
            if process.create_time() != create_time:
                continue
            process.kill()
            if process.create_time() == create_time:
                process.wait(timeout=2)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.ZombieProcess):
            continue


def _write_process_tree_command(tmp_path: Path, name: str) -> tuple[str, Path]:
    command_script = (tmp_path / f"{name}.py").resolve()
    descendant_script = (tmp_path / f"{name}-descendant.py").resolve()
    identity_path = (tmp_path / f"{name}.json").resolve()
    descendant_script.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    command_script.write_text(
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "import psutil\n"
        "\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, sys.argv[1]],\n"
        ")\n"
        "marker_path = Path(sys.argv[2])\n"
        "identities = {\n"
        "    'script': {'pid': os.getpid(), 'create_time': psutil.Process().create_time()},\n"
        "    'descendant': {'pid': descendant.pid, 'create_time': psutil.Process(descendant.pid).create_time()},\n"
        "}\n"
        "temporary_path = marker_path.with_name(f'{marker_path.name}.{os.getpid()}.tmp')\n"
        "temporary_path.write_text(json.dumps(identities), encoding='ascii')\n"
        "os.replace(temporary_path, marker_path)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return _python_command(command_script, descendant_script, identity_path), identity_path


async def _seed_cancel_round(runtime, tmp_path: Path, case: str) -> tuple[int, int, int, str, int, int, str, str]:
    child_instruction = "Run the assigned shell command and return only a concise verified summary."
    sibling_instruction = "Remain queued as an independent sibling task."
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME, SHELL_TOOL_NAME, TERMINAL_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME]))
        profile.configs = configs
        await db.commit()

    runtime.outputs.extend(
        [
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(id=f"cancel-create-a-{case}", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "create", "name": "cancel-child-a", "message": child_instruction}),
                    InternalToolCall(id=f"cancel-create-b-{case}", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "create", "name": "cancel-child-b", "message": sibling_instruction}),
                ],
            ),
        ]
    )
    accepted = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": f"subagent-shell-cancel-create-{case}",
            "message": "Create child A and sibling child B, then wait for their results.",
        },
    )
    assert accepted.status_code == 200
    parent_work_id = accepted.json()["data"]["work_id"]
    parent_done = await runtime.run_next(independent_only=False)
    assert parent_done is not None and parent_done.id == parent_work_id and parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

    async with runtime.factory() as db:
        children = (
            (
                await db.execute(
                    select(SessionReplyWorkItem)
                    .where(
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                        SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                        SessionReplyWorkItem.source_id == str(parent_work_id),
                    )
                    .order_by(SessionReplyWorkItem.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 2
        children_by_name = {}
        for child_work in children:
            assert child_work.id is not None and child_work.subagent_id is not None
            subagent = await db.get(Subagent, child_work.subagent_id)
            assert subagent is not None
            children_by_name[subagent.name] = (child_work, subagent)
        assert set(children_by_name) == {"cancel-child-a", "cancel-child-b"}
        child, child_subagent = children_by_name["cancel-child-a"]
        sibling, sibling_subagent = children_by_name["cancel-child-b"]
        assert child.id is not None and sibling.id is not None
        assert child.subagent_id == child_subagent.id
        assert sibling.subagent_id == sibling_subagent.id
        assert sibling.status == SessionReplyWorkStatus.READY_FOR_LLM
        sibling.available_at += 300
        parent_task = BackgroundTask(
            uid=runtime.uid,
            session_id=runtime.session_id,
            profile_id=runtime.profile.id,
            tool_call_id=f"parent-resource-{case}",
            tool_name=SHELL_TOOL_NAME,
            arguments={"command": "parent-resource", "execution_mode": "non_interactive"},
            auto_reply=False,
            extra={"origin_work_id": parent_work_id, "scope": "parent"},
        )
        sibling_task = BackgroundTask(
            uid=runtime.uid,
            session_id=runtime.session_id,
            profile_id=runtime.profile.id,
            tool_call_id=f"sibling-resource-{case}",
            tool_name=SHELL_TOOL_NAME,
            arguments={"command": "sibling-resource", "execution_mode": "non_interactive"},
            auto_reply=False,
            extra={"origin_work_id": sibling.id, "scope": "sibling"},
        )
        parent_terminal_id = f"parent-terminal-{case}".ljust(32, "p")
        sibling_terminal_id = f"sibling-terminal-{case}".ljust(32, "s")
        db.add_all(
            [
                parent_task,
                sibling_task,
                TerminalSession(
                    terminal_session_id=parent_terminal_id,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    original_tool_call_id=f"parent-terminal-call-{case}",
                    profile_id=runtime.profile.id,
                    command="parent terminal command",
                    working_directory=str(tmp_path),
                    status=TerminalSessionStatus.STARTING,
                    allowed_actions=[action.value for action in TerminalAction],
                ),
                TerminalSession(
                    terminal_session_id=sibling_terminal_id,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    original_tool_call_id=f"sibling-terminal-call-{case}",
                    profile_id=runtime.profile.id,
                    command="sibling terminal command",
                    working_directory=str(tmp_path),
                    status=TerminalSessionStatus.STARTING,
                    allowed_actions=[action.value for action in TerminalAction],
                ),
            ]
        )
        await db.commit()
        assert parent_task.id is not None and sibling_task.id is not None
        return parent_work_id, child.id, sibling.id, child_subagent.id, parent_task.id, sibling_task.id, parent_terminal_id, sibling_terminal_id


@pytest.mark.parametrize("execution_mode", ["non_interactive", "interactive"], ids=["non-interactive", "interactive"])
@pytest.mark.asyncio
async def test_subagent_shell_workflow_executes_recovers_and_reports_to_parent(image_chat_runtime, execution_mode: str) -> None:
    output_marker = f"SUBAGENT_SHELL_OUTPUT_{execution_mode.upper()}_A92C"
    child_summary = f"SUBAGENT_CHILD_SUMMARY_{execution_mode.upper()}_D64E"
    parent_summary = f"PARENT_FINAL_SUMMARY_{execution_mode.upper()}_B18A"
    command = f"echo {output_marker}"
    child_instruction = "Run one harmless echo command and return a concise summary of the verified result."

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(
                dict.fromkeys(
                    [
                        *configs["tool"]["enabled_tools"],
                        AGENT_WORK_TOOL_NAME,
                        SHELL_TOOL_NAME,
                    ]
                )
            )
            profile.configs = configs
            await db.commit()

        create_call = InternalToolCall(
            id=f"parent-create-{execution_mode}",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "create",
                "name": f"Shell workflow {execution_mode}",
                "message": child_instruction,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]),
            ]
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"subagent-shell-workflow-{execution_mode}",
                "message": f"Create the independent shell task for {execution_mode}.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]

        parent_submitted = await runtime.run_next(independent_only=False)
        assert parent_submitted is not None
        assert parent_submitted.id == parent_work_id
        assert parent_submitted.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = await _child_for_parent(db, parent_work_id)
            assert child.id is not None
            child_work_id = child.id
            assert child.subagent_id is not None
            parent_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert parent_call.child_work_id == child_work_id

        child_shell_call = InternalToolCall(
            id=f"child-shell-{execution_mode}",
            name=SHELL_TOOL_NAME,
            arguments={"command": command, "execution_mode": execution_mode},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[child_shell_call]))
        child_initial = await runtime.run_next(independent_only=True)
        assert child_initial is not None
        assert child_initial.id == child_work_id
        assert child_initial.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        if execution_mode == "non_interactive":
            async with runtime.factory() as db:
                shell_tasks = list(
                    (
                        await db.execute(
                            select(BackgroundTask)
                            .where(
                                BackgroundTask.session_id == runtime.session_id,
                                BackgroundTask.tool_name == SHELL_TOOL_NAME,
                            )
                            .order_by(BackgroundTask.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                shell_call_messages = list(
                    (
                        await db.execute(
                            select(Message).where(
                                Message.work_id == child_work_id,
                                Message.type == MessageType.TOOL_CALL,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(shell_tasks) == 1
            shell_task = shell_tasks[0]
            assert shell_task.id is not None
            assert shell_task.status == BackgroundTaskStatus.PENDING
            persisted_shell_calls = [tool_call for message in shell_call_messages for tool_call in (InternalMessage.model_validate_json(message.content or "{}").tool_calls or []) if tool_call.name == SHELL_TOOL_NAME and tool_call.arguments == child_shell_call.arguments]
            assert len(persisted_shell_calls) == 1
            assert shell_task.tool_call_id == persisted_shell_calls[0].id
            assert shell_task.extra["origin_work_id"] == child_work_id

            await runner_module.run_background_task(shell_task.id, worker_id=f"subagent-shell-{execution_mode}")

            async with runtime.factory() as db:
                completed_task = await db.get(BackgroundTask, shell_task.id)
                resumed_child = await db.get(SessionReplyWorkItem, child_work_id)
            assert completed_task is not None
            assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.result is not None
            assert output_marker in completed_task.result["content"]["stdout"]
            assert resumed_child is not None
            assert resumed_child.status == SessionReplyWorkStatus.READY_FOR_LLM

            child_resume_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child_work_id
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
            child_resume_dump = json.dumps(runtime.provider_payloads[child_resume_start:], ensure_ascii=False)
            assert output_marker in child_resume_dump
        else:
            async with runtime.factory() as db:
                auto_tasks = list(
                    (
                        await db.execute(
                            select(BackgroundTask)
                            .where(
                                BackgroundTask.session_id == runtime.session_id,
                                BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                                BackgroundTask.extra["auto_terminal_watch"].as_boolean().is_(True),
                            )
                            .order_by(BackgroundTask.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(auto_tasks) == 1
                auto_task = auto_tasks[0]
                assert auto_task.id is not None
                assert auto_task.status == BackgroundTaskStatus.PENDING
                assert auto_task.extra["origin_work_id"] == child_work_id
                terminal_session_id = auto_task.extra["terminal_session_id"]
                terminal_session = await db.get(TerminalSession, terminal_session_id)
                assert terminal_session is not None
                assert terminal_session.command == command
                terminal_output = f"{output_marker}\n"
                terminal_output_size = len(terminal_output.encode("utf-8"))
                terminal_session.status = TerminalSessionStatus.EXITED
                terminal_session.exit_code = 0
                terminal_session.failure_reason = None
                terminal_session.oldest_output_offset = 0
                terminal_session.next_output_offset = terminal_output_size
                terminal_session.oldest_output_sequence = 1
                terminal_session.next_output_sequence = 2
                db.add(terminal_session)
                await db.commit()

                worker_id = f"subagent-terminal-watch-{execution_mode}"
                claimed_auto = await background_task_crud.try_claim(
                    db,
                    task_id=auto_task.id,
                    worker_id=worker_id,
                    lease_seconds=300,
                )
                assert claimed_auto is not None
                assert await complete_terminal_watch_round(
                    db,
                    task=claimed_auto,
                    worker_id=worker_id,
                    content={
                        "terminal_session_id": terminal_session_id,
                        "event": "process_exit",
                        "after_offset": 0,
                        "session_status": TerminalSessionStatus.EXITED.value,
                        "exit_code": 0,
                        "failure_reason": None,
                        "output": terminal_output,
                        "start_offset": 0,
                        "next_offset": terminal_output_size,
                        "latest_offset": terminal_output_size,
                        "output_truncated": False,
                        "eof": True,
                        "condition": "process_exit",
                        "matched": True,
                    },
                )
                await db.refresh(claimed_auto)
                assert await route_terminal_auto_watch_reply(db, claimed_auto)

            async with runtime.factory() as db:
                resumed_child = await db.get(SessionReplyWorkItem, child_work_id)
                completed_auto = await db.get(BackgroundTask, auto_task.id)
            assert resumed_child is not None
            assert resumed_child.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert completed_auto is not None
            assert completed_auto.status == BackgroundTaskStatus.SUCCEEDED

            terminal_read_call = InternalToolCall(
                id=f"child-terminal-read-{execution_mode}",
                name=TERMINAL_TOOL_NAME,
                arguments={
                    "operation": "read",
                    "terminal_session_id": terminal_session_id,
                    "read_output": False,
                },
            )
            child_resume_start = len(runtime.provider_payloads)
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[terminal_read_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content=child_summary),
                ]
            )
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == child_work_id
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
            child_resume_dump = json.dumps(runtime.provider_payloads[child_resume_start:], ensure_ascii=False)
            assert output_marker in child_resume_dump
            assert terminal_session_id in child_resume_dump
            assert "output_buffer" in child_resume_dump
            assert TerminalSessionStatus.EXITED.value in child_resume_dump

        async with runtime.factory() as db:
            completed_child = await db.get(SessionReplyWorkItem, child_work_id)
            completed_parent_call = await db.get(SubagentToolCall, parent_call.id)
            assert completed_child is not None
            assert completed_child.status == SessionReplyWorkStatus.SUCCEEDED
            assert completed_parent_call is not None
            assert completed_parent_call.delivered is True

        parent_resume_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_summary))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_resume_dump = json.dumps(runtime.provider_payloads[parent_resume_start:], ensure_ascii=False)
        assert child_summary in parent_resume_dump
        assert command not in parent_resume_dump
        assert output_marker not in parent_resume_dump


@pytest.mark.asyncio
async def test_shell_timeout_cleans_process_tree(tmp_path: Path) -> None:
    timeout = 3.0
    command, identity_path = _write_process_tree_command(tmp_path, "timeout")
    executor = ShellExecutor(project_root=str(tmp_path), uid="shell-timeout", working_directory=tmp_path)
    executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": timeout}}))
    execute_task: asyncio.Task[str] | None = None

    try:
        execute_task = asyncio.create_task(executor.execute(command=command, execution_mode="non_interactive"))
        await _wait_for_path(identity_path)
        process_identities = _read_process_identities(identity_path)
        for process_pid, process_create_time in process_identities.values():
            assert _process_is_running(process_pid, process_create_time)

        result = json.loads(await asyncio.wait_for(execute_task, timeout=10))
        assert result["error"] == t(ERR_TOOL_COMMAND_TIMEOUT, timeout=timeout)
        assert result["system_info"]
        assert "exit_code" not in result
        for process_pid, process_create_time in process_identities.values():
            await _wait_for_process_exit(process_pid, process_create_time)
    finally:
        if execute_task is not None and not execute_task.done():
            execute_task.cancel()
        if execute_task is not None:
            with contextlib.suppress(BaseException):
                await execute_task
        _kill_process_identities(identity_path)


@pytest.mark.parametrize(
    "case",
    ["non_interactive_pending", "non_interactive_running", "interactive_starting"],
    ids=["non-interactive-pending", "non-interactive-running", "interactive-starting"],
)
@pytest.mark.asyncio
async def test_subagent_shell_cancel_cleans_only_current_round_resources(
    image_chat_runtime,
    tmp_path: Path,
    case: str,
) -> None:
    execution_mode = "interactive" if case == "interactive_starting" else "non_interactive"
    running = case == "non_interactive_running"
    command, identity_path = _write_process_tree_command(tmp_path, f"cancel-{case}")
    cancel_final = f"parent final after cancelling {case}"
    runner_task: asyncio.Task | None = None
    process_identities: dict[str, tuple[int, float]] | None = None

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        try:
            (
                parent_work_id,
                child_work_id,
                sibling_work_id,
                child_subagent_id,
                parent_task_id,
                sibling_task_id,
                parent_terminal_id,
                sibling_terminal_id,
            ) = await _seed_cancel_round(runtime, tmp_path, case)

            shell_call = InternalToolCall(
                id=f"cancel-shell-{case}",
                name=SHELL_TOOL_NAME,
                arguments={"command": command, "execution_mode": execution_mode},
            )
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[shell_call]))
            child_waiting = await runtime.run_next(independent_only=True)
            assert child_waiting is not None
            assert child_waiting.id == child_work_id
            assert child_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                tasks = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id))).scalars().all()
                target_tasks = [task for task in tasks if isinstance(task.extra, dict) and task.extra.get("origin_work_id") == child_work_id]
                assert len(target_tasks) == 1
                target_task = target_tasks[0]
                assert target_task.id is not None
                target_task_id = target_task.id
                assert target_task.status == BackgroundTaskStatus.PENDING
                if execution_mode == "non_interactive":
                    assert target_task.tool_name == SHELL_TOOL_NAME
                    assert target_task.arguments == shell_call.arguments
                else:
                    assert target_task.tool_name == TERMINAL_WATCH_TOOL_NAME
                    assert target_task.extra.get("auto_terminal_watch") is True
                    target_terminal_id = target_task.extra.get("terminal_session_id")
                    assert isinstance(target_terminal_id, str)
                    target_terminal = await db.get(TerminalSession, target_terminal_id)
                    assert target_terminal is not None
                    assert target_terminal.status == TerminalSessionStatus.STARTING
                    assert target_terminal.command == command

            if running:
                runner_task = asyncio.create_task(runner_module.run_background_task(target_task_id, worker_id=f"cancel-shell-{case}"))
                await _wait_for_path(identity_path)
                process_identities = _read_process_identities(identity_path)
                for process_pid, process_create_time in process_identities.values():
                    assert _process_is_running(process_pid, process_create_time)
                async with runtime.factory() as db:
                    running_task = await db.get(BackgroundTask, target_task_id)
                    assert running_task is not None
                    assert running_task.status == BackgroundTaskStatus.RUNNING

            cancel_call = InternalToolCall(
                id=f"cancel-call-{case}",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "cancel",
                    "subagent_id": child_subagent_id,
                    "work_id": child_work_id,
                },
            )
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content=cancel_final),
                ]
            )
            cancel_accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": f"subagent-shell-cancel-request-{case}",
                    "message": f"Cancel child {child_work_id} and finish the parent reply.",
                },
            )
            assert cancel_accepted.status_code == 200
            cancel_data = cancel_accepted.json()["data"]
            assert cancel_data["submission_status"] == "accepted"
            cancel_provider_start = len(runtime.provider_payloads)
            cancel_done = await runtime.run_next(independent_only=False)
            assert cancel_done is not None
            assert cancel_done.id == cancel_data["work_id"]
            assert cancel_done.status == SessionReplyWorkStatus.SUCCEEDED
            cancel_provider_dump = json.dumps(runtime.provider_payloads[cancel_provider_start:], ensure_ascii=False)
            assert t(ERR_SUBAGENT_CANCELLED) in cancel_provider_dump
            assert command not in cancel_provider_dump
            provider_count_after_cancel = len(runtime.provider_payloads)

            if running:
                assert runner_task is not None
                await asyncio.wait_for(runner_task, timeout=10)
                runner_task = None
                assert process_identities is not None
                for process_pid, process_create_time in process_identities.values():
                    await _wait_for_process_exit(process_pid, process_create_time)
            else:
                await runner_module.run_background_task(target_task_id, worker_id=f"cancel-after-{case}")
                assert not identity_path.exists()

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                cancelled_child = await db.get(SessionReplyWorkItem, child_work_id)
                unchanged_sibling = await db.get(SessionReplyWorkItem, sibling_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                cancelled_task = await db.get(BackgroundTask, target_task_id)
                unchanged_parent_task = await db.get(BackgroundTask, parent_task_id)
                unchanged_sibling_task = await db.get(BackgroundTask, sibling_task_id)
                stored_parent_terminal = await db.get(TerminalSession, parent_terminal_id)
                stored_sibling_terminal = await db.get(TerminalSession, sibling_terminal_id)
                assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert cancelled_child is not None and cancelled_child.status == SessionReplyWorkStatus.CANCELLED
                assert unchanged_sibling is not None and unchanged_sibling.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert round_row is not None and round_row.result is not None
                assert round_row.result["completed"] is True
                assert round_row.result["status"] == "cancelled"
                assert round_row.result["error"] == t(ERR_SUBAGENT_CANCELLED)
                assert cancelled_task is not None and cancelled_task.status == BackgroundTaskStatus.CANCELLED
                assert unchanged_parent_task is not None and unchanged_parent_task.status == BackgroundTaskStatus.PENDING
                assert unchanged_sibling_task is not None and unchanged_sibling_task.status == BackgroundTaskStatus.PENDING
                assert stored_parent_terminal is not None and stored_parent_terminal.status == TerminalSessionStatus.STARTING
                assert stored_sibling_terminal is not None and stored_sibling_terminal.status == TerminalSessionStatus.STARTING
                if execution_mode == "interactive":
                    assert await db.get(TerminalSession, target_terminal_id) is None
                    assert cancelled_task.extra.get("auto_terminal_watch") is True

                create_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id, SubagentToolCall.child_work_id == child_work_id))).scalars().one()
                assert create_call.delivered is True
                assert create_call.tool_result_message_id is not None
                result_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.uid == runtime.uid,
                                Message.session_id == runtime.session_id,
                                Message.profile_id == runtime.profile.id,
                                Message.work_id.is_(None),
                                Message.role == MessageRole.TOOL,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                cancellation_rows = []
                for row in result_rows:
                    stored_message = InternalMessage.model_validate_json(row.content or "{}")
                    payload = json.loads(stored_message.content or "{}")
                    if stored_message.tool_call_id == create_call.tool_call_id:
                        cancellation_rows.append((row, stored_message, payload))
                assert len(cancellation_rows) == 1
                result_row, result_message, result_payload = cancellation_rows[0]
                assert result_row.id == create_call.tool_result_message_id
                assert result_message.tool_call_id == create_call.tool_call_id
                assert result_message.tool_execution_status == "failed"
                assert result_payload == {
                    "subagent_id": create_call.subagent_id,
                    "work_id": child_work_id,
                    "status": "cancelled",
                    "reason": t(ERR_SUBAGENT_CANCELLED),
                }
                assert command not in json.dumps(result_payload, ensure_ascii=False)

            assert len(runtime.provider_payloads) == provider_count_after_cancel
            if execution_mode == "interactive":
                assert not identity_path.exists()

            parent_provider_start = len(runtime.provider_payloads)
            parent_final = f"original parent resumed after cancelling {case}"
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await runtime.run_next(independent_only=False)
            assert parent_done is not None
            assert parent_done.id == parent_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            parent_provider_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
            assert t(ERR_SUBAGENT_CANCELLED) in parent_provider_dump
            assert command not in parent_provider_dump
            history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert any(item.get("content") == cancel_final for item in history)
            assert any(item.get("content") == parent_final for item in history)
            assert command not in json.dumps(history, ensure_ascii=False)
        finally:
            if runner_task is not None and not runner_task.done():
                runner_task.cancel()
            if runner_task is not None:
                with contextlib.suppress(BaseException):
                    await runner_task
            _kill_process_identities(identity_path)


@pytest.mark.asyncio
async def test_subagent_shell_cancel_fails_when_interactive_identity_is_missing(image_chat_runtime, tmp_path: Path) -> None:
    case = "interactive_missing_identity"
    command = "interactive identity capture failure"
    expected_error = t(ERR_TERMINAL_COMMAND_FAILED, error="invalid process identity")

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        (
            parent_work_id,
            child_work_id,
            _sibling_work_id,
            child_subagent_id,
            _parent_task_id,
            _sibling_task_id,
            _parent_terminal_id,
            _sibling_terminal_id,
        ) = await _seed_cancel_round(runtime, tmp_path, case)

        shell_call = InternalToolCall(
            id=f"cancel-shell-{case}",
            name=SHELL_TOOL_NAME,
            arguments={"command": command, "execution_mode": "interactive"},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[shell_call]))
        child_waiting = await runtime.run_next(independent_only=True)
        assert child_waiting is not None
        assert child_waiting.id == child_work_id
        assert child_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            tasks = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == runtime.session_id).order_by(BackgroundTask.id))).scalars().all()
            target_tasks = [task for task in tasks if isinstance(task.extra, dict) and task.extra.get("origin_work_id") == child_work_id]
            assert len(target_tasks) == 1
            target_task = target_tasks[0]
            assert target_task.id is not None
            target_task_id = target_task.id
            assert target_task.tool_name == TERMINAL_WATCH_TOOL_NAME
            assert target_task.status == BackgroundTaskStatus.PENDING
            assert target_task.extra.get("auto_terminal_watch") is True
            target_terminal_id = target_task.extra.get("terminal_session_id")
            assert isinstance(target_terminal_id, str)
            target_terminal = await db.get(TerminalSession, target_terminal_id)
            assert target_terminal is not None
            assert target_terminal.status == TerminalSessionStatus.STARTING
            assert target_terminal.process_identity is None
            assert target_terminal.command == command
            target_terminal.status = TerminalSessionStatus.RUNNING
            target_terminal.process_identity = None
            db.add(target_terminal)
            await db.commit()

        cancel_call = InternalToolCall(
            id=f"cancel-call-{case}",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "cancel",
                "subagent_id": child_subagent_id,
                "work_id": child_work_id,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="parent reply after failed cancellation"),
            ]
        )
        cancel_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"subagent-shell-cancel-request-{case}",
                "message": f"Cancel child {child_work_id} and report the cancellation result.",
            },
        )
        assert cancel_accepted.status_code == 200
        cancel_data = cancel_accepted.json()["data"]
        assert cancel_data["submission_status"] == "accepted"
        cancel_done = await runtime.run_next(independent_only=False)
        assert cancel_done is not None
        assert cancel_done.id == cancel_data["work_id"]
        assert cancel_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert cancel_done.input_message_ids is not None
        assert len(cancel_done.input_message_ids) == 1
        cancel_input_message_id = cancel_done.input_message_ids[-1]

        async with runtime.factory() as db:
            cancel_result_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.uid == runtime.uid,
                            Message.session_id == runtime.session_id,
                            Message.profile_id == runtime.profile.id,
                            Message.work_id.is_(None),
                            Message.id > cancel_input_message_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(cancel_result_rows) == 1
            failed_message = InternalMessage.model_validate_json(cancel_result_rows[0].content or "{}")
            failed_payload = json.loads(failed_message.content or "{}")
            assert failed_message.tool_execution_status == "failed"
            assert failed_payload["status"] == "failed"
            assert failed_payload["error"] == expected_error

            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
            subagent = await db.get(Subagent, child_subagent_id)
            watch_task = await db.get(BackgroundTask, target_task_id)
            terminal = await db.get(TerminalSession, target_terminal_id)
            create_call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.child_work_id == child_work_id,
                        )
                    )
                )
                .scalars()
                .one()
            )

        assert parent is not None
        assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert child is not None
        assert child.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert round_row is not None
        assert round_row.result is None
        assert round_row.current_work_id == child_work_id
        assert subagent is not None
        assert subagent.active_child_work_id == child_work_id
        assert watch_task is not None
        assert watch_task.status == BackgroundTaskStatus.PENDING
        assert terminal is not None
        assert terminal.status == TerminalSessionStatus.RUNNING
        assert terminal.process_identity is None
        assert terminal.command == command
        assert create_call.delivered is False
