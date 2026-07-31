import math
import sys
from dataclasses import FrozenInstanceError

import pytest

from app.core.terminal import pty_factory
from app.core.terminal.pty_base import (
    BoundedPtyOutputBuffer,
    PtyDriver,
    PtyProcessConfig,
)
from app.core.terminal.schemas import TerminalSignal


def make_config(**overrides: object) -> PtyProcessConfig:
    values: dict[str, object] = {
        "argv": ("python", "-c", "pass"),
        "cwd": ".",
        "env": {"TERM": "x"},
    }
    values.update(overrides)
    return PtyProcessConfig(**values)


def test_process_config_defensively_copies_and_freezes_env() -> None:
    source = {"TERM": "x"}
    config = make_config(env=source)

    source["TERM"] = "changed"
    source["NEW"] = "value"

    assert dict(config.env) == {"TERM": "x"}
    with pytest.raises(TypeError):
        config.env["TERM"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        config.cwd = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("argv", ()),
        ("argv", ["python"]),
        ("argv", ("",)),
        ("argv", ("python", 1)),
        ("cwd", ""),
        ("cwd", None),
        ("env", []),
        ("env", {"": "value"}),
        ("env", {"KEY": 1}),
        ("env", {"KEY\x00": "value"}),
        ("env", {"KEY": "value\x00"}),
    ],
)
def test_process_config_rejects_invalid_argv_cwd_env_and_nul(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        make_config(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("columns", 0),
        ("columns", 1_001),
        ("columns", True),
        ("columns", 1.0),
        ("rows", 0),
        ("rows", 1_001),
        ("rows", False),
        ("rows", 24.0),
    ],
)
def test_process_config_rejects_invalid_dimensions(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        make_config(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_capacity_bytes", 0),
        ("output_capacity_bytes", -1),
        ("output_capacity_bytes", True),
        ("output_capacity_bytes", 1.0),
        ("read_chunk_bytes", 0),
        ("read_chunk_bytes", -1),
        ("read_chunk_bytes", False),
        ("read_chunk_bytes", 1.0),
    ],
)
def test_process_config_rejects_invalid_capacity_and_read_chunk(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        make_config(**{field: value})


@pytest.mark.parametrize("value", [-0.1, math.inf, -math.inf, math.nan, True, "3"])
def test_process_config_rejects_invalid_close_grace(value: object) -> None:
    with pytest.raises(ValueError):
        make_config(close_grace_seconds=value)


def test_process_config_accepts_inclusive_numeric_boundaries() -> None:
    config = make_config(
        columns=1_000,
        rows=1,
        output_capacity_bytes=1,
        read_chunk_bytes=1,
        close_grace_seconds=0,
    )

    assert config.columns == 1_000
    assert config.rows == 1
    assert config.output_capacity_bytes == 1
    assert config.read_chunk_bytes == 1
    assert config.close_grace_seconds == 0


def test_output_buffer_initial_state() -> None:
    buffer = BoundedPtyOutputBuffer(4)

    assert buffer.retained_bytes == 0
    assert buffer.dropped_bytes == 0
    assert buffer.oldest_offset == 0
    assert buffer.latest_offset == 0
    assert buffer.state().model_dump() == {
        "capacity_bytes": 4,
        "oldest_offset": 0,
        "next_offset": 0,
        "oldest_sequence": 1,
        "next_sequence": 1,
    }


def test_output_buffer_reads_multiple_blocks_by_absolute_offset() -> None:
    buffer = BoundedPtyOutputBuffer(32)
    buffer.append(b"ab")
    buffer.append(b"cde")
    buffer.append(b"fg")

    first_page = buffer.read(1, 4)
    second_page = buffer.read(2, 5)

    assert first_page.data == b"bcde"
    assert first_page.requested_offset == 1
    assert first_page.start_offset == 1
    assert first_page.next_offset == 5
    assert first_page.sequence == 2
    assert first_page.truncated is False

    assert second_page.data == b"cdefg"
    assert second_page.start_offset == 2
    assert second_page.next_offset == 7
    assert second_page.sequence == 3


def test_output_buffer_capacity_drops_complete_oldest_blocks() -> None:
    buffer = BoundedPtyOutputBuffer(4)
    buffer.append(b"ab")
    buffer.append(b"cd")
    buffer.append(b"ef")

    assert buffer.retained_bytes == 4
    assert buffer.dropped_bytes == 2
    assert buffer.oldest_offset == 2
    assert buffer.latest_offset == 6
    assert buffer.state().model_dump() == {
        "capacity_bytes": 4,
        "oldest_offset": 2,
        "next_offset": 6,
        "oldest_sequence": 2,
        "next_sequence": 4,
    }

    read = buffer.read(0, 10)
    assert read.data == b"cdef"
    assert read.start_offset == 2
    assert read.next_offset == 6
    assert read.oldest_available_offset == 2
    assert read.latest_offset == 6
    assert read.sequence == 3
    assert read.truncated is True


def test_output_buffer_capacity_truncates_in_the_middle_of_oldest_block() -> None:
    buffer = BoundedPtyOutputBuffer(5)
    buffer.append(b"abc")
    buffer.append(b"defg")

    assert buffer.retained_bytes == 5
    assert buffer.dropped_bytes == 2
    assert buffer.oldest_offset == 2
    assert buffer.latest_offset == 7
    assert buffer.state().model_dump() == {
        "capacity_bytes": 5,
        "oldest_offset": 2,
        "next_offset": 7,
        "oldest_sequence": 1,
        "next_sequence": 3,
    }

    read = buffer.read(2, 5)
    assert read.data == b"cdefg"
    assert read.start_offset == 2
    assert read.next_offset == 7
    assert read.sequence == 2
    assert read.truncated is False


def test_output_buffer_oversized_single_block_keeps_only_tail() -> None:
    buffer = BoundedPtyOutputBuffer(4)
    buffer.append(b"012345")

    assert buffer.retained_bytes == 4
    assert buffer.dropped_bytes == 2
    assert buffer.oldest_offset == 2
    assert buffer.latest_offset == 6
    assert buffer.state().model_dump() == {
        "capacity_bytes": 4,
        "oldest_offset": 2,
        "next_offset": 6,
        "oldest_sequence": 1,
        "next_sequence": 2,
    }

    read = buffer.read(0, 4)
    assert read.data == b"2345"
    assert read.start_offset == 2
    assert read.next_offset == 6
    assert read.sequence == 1
    assert read.truncated is True


def test_output_buffer_repeated_offset_reads_are_idempotent() -> None:
    buffer = BoundedPtyOutputBuffer(8)
    buffer.append(b"abcdef")

    first = buffer.read(2, 3)
    second = buffer.read(2, 3)

    assert first == second
    assert buffer.oldest_offset == 0
    assert buffer.latest_offset == 6


def test_output_buffer_empty_read_and_eof_are_returned() -> None:
    buffer = BoundedPtyOutputBuffer(4)

    empty = buffer.read(0, 4, eof=True)

    assert empty.data == b""
    assert empty.requested_offset == 0
    assert empty.start_offset == 0
    assert empty.next_offset == 0
    assert empty.sequence == 0
    assert empty.truncated is False
    assert empty.eof is True

    buffer.append(b"out")
    at_end = buffer.read(3, 4, eof=False)
    assert at_end.data == b""
    assert at_end.start_offset == 3
    assert at_end.next_offset == 3
    assert at_end.sequence == 1
    assert at_end.eof is False


@pytest.mark.parametrize("offset", [-1, True, False, 1.0, "0", None])
def test_output_buffer_rejects_invalid_offsets(offset: object) -> None:
    buffer = BoundedPtyOutputBuffer(4)

    with pytest.raises(ValueError):
        buffer.read(offset, 1)  # type: ignore[arg-type]


def test_output_buffer_rejects_offset_beyond_latest() -> None:
    buffer = BoundedPtyOutputBuffer(4)
    buffer.append(b"out")

    with pytest.raises(ValueError):
        buffer.read(4, 1)


@pytest.mark.parametrize("max_bytes", [0, -1, True, False, 1.0, "1", None])
def test_output_buffer_rejects_invalid_max_bytes(max_bytes: object) -> None:
    buffer = BoundedPtyOutputBuffer(4)

    with pytest.raises(ValueError):
        buffer.read(0, max_bytes)  # type: ignore[arg-type]


class FakePtyDriver(PtyDriver):
    def __init__(
        self,
        config: PtyProcessConfig,
        *,
        process_id: int | None = 123,
        process_running: bool = True,
        stream_eof: bool = False,
        process_exit_code: int | None = None,
    ) -> None:
        super().__init__(config)
        self._process_id = process_id
        self._process_running = process_running
        self._stream_eof = stream_eof
        self._process_exit_code = process_exit_code

    async def start(self) -> None:
        return None

    async def write(self, data: str) -> int:
        return len(data)

    async def resize(self, columns: int, rows: int) -> None:
        return None

    async def send_signal(self, signal: TerminalSignal) -> None:
        return None

    async def wait(self) -> int:
        return self._process_exit_code or 0

    async def close(self, force: bool = False) -> None:
        return None

    @property
    def pid(self) -> int | None:
        return self._process_id

    @property
    def running(self) -> bool:
        return self._process_running

    @property
    def eof(self) -> bool:
        return self._stream_eof

    @property
    def exit_code(self) -> int | None:
        return self._process_exit_code

    def emit(self, data: bytes) -> None:
        self._output_buffer.append(data)


def test_resource_snapshot_uses_fake_driver_and_output_state() -> None:
    driver = FakePtyDriver(
        make_config(output_capacity_bytes=3),
        process_id=456,
        process_running=False,
        stream_eof=True,
        process_exit_code=7,
    )
    driver.emit(b"abcd")

    snapshot = driver.resource_snapshot()

    assert snapshot.pid == 456
    assert snapshot.running is False
    assert snapshot.eof is True
    assert snapshot.exit_code == 7
    assert snapshot.retained_bytes == 3
    assert snapshot.dropped_bytes == 1
    assert snapshot.output_buffer.model_dump() == {
        "capacity_bytes": 3,
        "oldest_offset": 1,
        "next_offset": 4,
        "oldest_sequence": 1,
        "next_sequence": 2,
    }


def test_factory_returns_driver_for_current_platform() -> None:
    config = make_config()
    driver = pty_factory.create_pty_driver(config)

    if sys.platform == "win32":
        from app.core.terminal.pty_windows import WindowsPtyDriver

        assert isinstance(driver, WindowsPtyDriver)
    elif sys.platform.startswith("linux"):
        from app.core.terminal.pty_unix import LinuxPtyDriver

        assert isinstance(driver, LinuxPtyDriver)
    else:
        pytest.fail(f"Unexpected unsupported test platform: {sys.platform}")


def test_factory_rejects_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pty_factory.sys, "platform", "darwin")

    with pytest.raises(RuntimeError):
        pty_factory.create_pty_driver(make_config())
