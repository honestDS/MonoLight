"""Platform-independent PTY process contracts and bounded output storage."""

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from app.core.constants import (
    ERR_TERMINAL_PTY_CONFIG_INVALID,
    ERR_TERMINAL_PTY_INPUT_INVALID,
    ERR_TERMINAL_READ_OFFSET_AHEAD,
)
from app.core.i18n import t
from app.core.terminal.schemas import TerminalOutputBufferState, TerminalSignal


@dataclass(frozen=True, slots=True)
class PtyProcessConfig:
    """Immutable configuration shared by platform-specific PTY drivers."""

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    columns: int = 80
    rows: int = 24
    output_capacity_bytes: int = 1_048_576
    read_chunk_bytes: int = 65_536
    close_grace_seconds: float = 3.0

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="argv"))
        if any(not isinstance(argument, str) or not argument for argument in self.argv):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="argv"))
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="cwd"))
        if not isinstance(self.env, Mapping):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="env"))
        env_copy = dict(self.env)
        if any(not isinstance(key, str) or not key or "\x00" in key or not isinstance(value, str) or "\x00" in value for key, value in env_copy.items()):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="env"))
        if not _is_valid_int(self.columns, minimum=1, maximum=1_000):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="columns"))
        if not _is_valid_int(self.rows, minimum=1, maximum=1_000):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="rows"))
        if not _is_valid_int(self.output_capacity_bytes, minimum=1):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="output_capacity_bytes"))
        if not _is_valid_int(self.read_chunk_bytes, minimum=1):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="read_chunk_bytes"))
        if not _is_valid_number(self.close_grace_seconds, minimum=0):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="close_grace_seconds"))

        object.__setattr__(self, "env", MappingProxyType(env_copy))


@dataclass(frozen=True, slots=True)
class PtyOutputRead:
    """A bounded, offset-addressed PTY output read."""

    requested_offset: int
    start_offset: int
    next_offset: int
    oldest_available_offset: int
    latest_offset: int
    sequence: int
    data: bytes
    truncated: bool
    eof: bool


@dataclass(frozen=True, slots=True)
class PtyResourceSnapshot:
    """Point-in-time PTY process and output resource state."""

    pid: int | None
    running: bool
    eof: bool
    exit_code: int | None
    output_buffer: TerminalOutputBufferState
    retained_bytes: int
    dropped_bytes: int


@dataclass(frozen=True, slots=True)
class _PtyOutputBlock:
    sequence: int
    start_offset: int
    data: bytes


class BoundedPtyOutputBuffer:
    """Retain the newest bytes of a monotonically growing PTY output stream."""

    __slots__ = ("_blocks", "_capacity_bytes", "_next_offset", "_next_sequence")

    def __init__(self, capacity_bytes: int) -> None:
        if not _is_valid_int(capacity_bytes, minimum=1):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="capacity_bytes"))
        self._capacity_bytes = capacity_bytes
        self._blocks: deque[_PtyOutputBlock] = deque()
        self._next_offset = 0
        self._next_sequence = 1

    @property
    def retained_bytes(self) -> int:
        """Return the number of currently retained output bytes."""
        return self._next_offset - self.oldest_offset

    @property
    def dropped_bytes(self) -> int:
        """Return the number of output bytes discarded from the beginning."""
        return self.oldest_offset

    @property
    def latest_offset(self) -> int:
        """Return the absolute offset after the newest appended byte."""
        return self._next_offset

    @property
    def oldest_offset(self) -> int:
        """Return the absolute offset of the oldest retained byte."""
        if not self._blocks:
            return self._next_offset
        return self._blocks[0].start_offset

    def append(self, data: bytes) -> None:
        """Append one output chunk and retain only the configured capacity."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(t(ERR_TERMINAL_PTY_INPUT_INVALID, field="output"))
        data_copy = bytes(data)
        if not data_copy:
            return

        block = _PtyOutputBlock(
            sequence=self._next_sequence,
            start_offset=self._next_offset,
            data=data_copy,
        )
        self._blocks.append(block)
        self._next_offset += len(data_copy)
        self._next_sequence += 1
        self._discard_excess_bytes()

    def read(self, offset: int, max_bytes: int, eof: bool = False) -> PtyOutputRead:
        """Read at most ``max_bytes`` beginning at an absolute offset."""
        if not _is_valid_int(offset, minimum=0):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="offset"))
        if not _is_valid_int(max_bytes, minimum=1):
            raise ValueError(t(ERR_TERMINAL_PTY_CONFIG_INVALID, field="max_bytes"))
        if offset > self._next_offset:
            raise ValueError(t(ERR_TERMINAL_READ_OFFSET_AHEAD))

        oldest_offset = self.oldest_offset
        start_offset = max(offset, oldest_offset)
        truncated = offset < oldest_offset
        remaining = max_bytes
        chunks: list[bytes] = []
        sequence = 0

        if start_offset < self._next_offset:
            for block in self._blocks:
                block_end = block.start_offset + len(block.data)
                if block_end <= start_offset:
                    continue
                block_start = max(start_offset, block.start_offset)
                block_index = block_start - block.start_offset
                chunk_size = min(remaining, block_end - block_start)
                chunks.append(block.data[block_index : block_index + chunk_size])
                sequence = block.sequence
                remaining -= chunk_size
                if remaining == 0:
                    break

        data = b"".join(chunks)
        if not data:
            sequence = self._next_sequence - 1 if self._next_sequence > 1 else 0

        return PtyOutputRead(
            requested_offset=offset,
            start_offset=start_offset,
            next_offset=start_offset + len(data),
            oldest_available_offset=oldest_offset,
            latest_offset=self._next_offset,
            sequence=sequence,
            data=data,
            truncated=truncated,
            eof=eof,
        )

    def state(self) -> TerminalOutputBufferState:
        """Return an immutable protocol snapshot of retained output bounds."""
        oldest_sequence = self._blocks[0].sequence if self._blocks else self._next_sequence
        return TerminalOutputBufferState(
            capacity_bytes=self._capacity_bytes,
            oldest_offset=self.oldest_offset,
            next_offset=self._next_offset,
            oldest_sequence=oldest_sequence,
            next_sequence=self._next_sequence,
        )

    def _discard_excess_bytes(self) -> None:
        excess_bytes = self.retained_bytes - self._capacity_bytes
        while excess_bytes > 0:
            oldest_block = self._blocks[0]
            if len(oldest_block.data) <= excess_bytes:
                self._blocks.popleft()
                excess_bytes -= len(oldest_block.data)
                continue

            self._blocks[0] = _PtyOutputBlock(
                sequence=oldest_block.sequence,
                start_offset=oldest_block.start_offset + excess_bytes,
                data=oldest_block.data[excess_bytes:],
            )
            excess_bytes = 0


class PtyDriver(ABC):
    """Abstract asynchronous PTY driver with shared output bookkeeping."""

    def __init__(self, config: PtyProcessConfig) -> None:
        self.config = config
        self._output_buffer = BoundedPtyOutputBuffer(config.output_capacity_bytes)

    @abstractmethod
    async def start(self) -> None:
        """Start the platform-specific PTY process."""
        raise NotImplementedError

    @abstractmethod
    async def write(self, data: str) -> int:
        """Write text to the PTY input stream."""
        raise NotImplementedError

    @abstractmethod
    async def resize(self, columns: int, rows: int) -> None:
        """Resize the platform-specific PTY."""
        raise NotImplementedError

    @abstractmethod
    async def send_signal(self, signal: TerminalSignal) -> None:
        """Send a platform-specific process signal."""
        raise NotImplementedError

    @abstractmethod
    async def wait(self) -> int:
        """Wait for the PTY process and return its exit code."""
        raise NotImplementedError

    @abstractmethod
    async def close(self, force: bool = False) -> None:
        """Close the PTY process."""
        raise NotImplementedError

    @property
    @abstractmethod
    def pid(self) -> int | None:
        """Return the process identifier, if one exists."""
        raise NotImplementedError

    @property
    @abstractmethod
    def running(self) -> bool:
        """Return whether the PTY process is running."""
        raise NotImplementedError

    @property
    @abstractmethod
    def eof(self) -> bool:
        """Return whether the PTY output stream reached EOF."""
        raise NotImplementedError

    @property
    @abstractmethod
    def exit_code(self) -> int | None:
        """Return the process exit code, if it has exited."""
        raise NotImplementedError

    def read_output(self, offset: int, max_bytes: int) -> PtyOutputRead:
        """Read output using the current EOF state."""
        return self._output_buffer.read(offset, max_bytes, eof=self.eof)

    def resource_snapshot(self) -> PtyResourceSnapshot:
        """Return the current process and output resource snapshot."""
        retained_bytes = self._output_buffer.retained_bytes
        return PtyResourceSnapshot(
            pid=self.pid,
            running=self.running,
            eof=self.eof,
            exit_code=self.exit_code,
            output_buffer=self._output_buffer.state(),
            retained_bytes=retained_bytes,
            dropped_bytes=self._output_buffer.dropped_bytes,
        )


def _is_valid_int(value: object, minimum: int, maximum: int | None = None) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        return False
    return maximum is None or value <= maximum


def _is_valid_number(value: object, minimum: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= minimum


__all__ = [
    "BoundedPtyOutputBuffer",
    "PtyDriver",
    "PtyOutputRead",
    "PtyProcessConfig",
    "PtyResourceSnapshot",
]
