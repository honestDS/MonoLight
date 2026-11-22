"""Immutable wire models and framing helpers for the notification protocol."""

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from app.core.constants import (
    NOTIFICATION_IO_TIMEOUT_SECONDS,
    NOTIFICATION_MAX_FRAME_BYTES,
    NOTIFICATION_MAX_SUBSCRIPTIONS,
    NOTIFICATION_PROTOCOL_VERSION,
)

_TOPIC_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
_CLIENT_ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"
_MACHINE_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


type NotificationTopicName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_TOPIC_PATTERN),
]
type NotificationIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
type NotificationClientIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_CLIENT_ID_PATTERN),
]
type NotificationMachineCode = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_MACHINE_CODE_PATTERN),
]
type NotificationToken = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class NotificationTopic(StrEnum):
    """Convenience names for established topics; wire topic fields stay open."""

    SESSION_REPLY = "session.reply"
    BACKGROUND_TASK = "task.background"
    SCHEDULED_TASK = "task.scheduled"
    MESSAGE_PLATFORM = "message_platform.config"
    OUTBOX = "message_platform.outbox"
    MEMORY_JOB = "memory.job"
    KNOWLEDGE_JOB = "knowledge.job"
    TERMINAL_SESSION = "terminal.session"
    TERMINAL_COMMAND = "terminal.command"
    SESSION_EVENT = "session.event"
    SYSTEM_SETTINGS = "system.settings"
    PROFILE_CONFIG = "profile.config"


class NotificationProtocolError(ValueError):
    """Protocol failure whose public message contains only a machine code."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or re.fullmatch(_MACHINE_CODE_PATTERN, code) is None:
            code = "invalid_message"
        self.code = code
        super().__init__(code)


class NotificationProtocolModel(BaseModel):
    """Shared immutable, strict configuration for notification models."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class NotificationScope(NotificationProtocolModel):
    uid: NotificationIdentifier | None = None
    session_id: NotificationIdentifier | None = None
    worker_id: NotificationIdentifier | None = None
    profile_id: int | None = Field(default=None, gt=0, strict=True)


class NotificationResource(NotificationProtocolModel):
    type: NotificationTopicName
    id: NotificationIdentifier


class Notification(NotificationProtocolModel):
    """A compact wake-up envelope containing references, not execution authority.

    Only resource references and wake-up hints belong here. This does not grant
    execution permission or carry raw terminal input or complete tool results.
    available_at is advisory; the center does not schedule execution.
    """

    notification_id: NotificationIdentifier = Field(default_factory=lambda: uuid4().hex)
    topic: NotificationTopicName
    event: NotificationTopicName
    source: NotificationClientIdentifier
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    available_at: AwareDatetime | None = None
    scope: NotificationScope = Field(default_factory=NotificationScope)
    resource: NotificationResource | None = None
    correlation_id: NotificationIdentifier | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class NotificationFrameBase(NotificationProtocolModel):
    version: Literal[1] = NOTIFICATION_PROTOCOL_VERSION

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, value: object) -> int:
        if type(value) is not int or value != NOTIFICATION_PROTOCOL_VERSION:
            raise ValueError("unsupported_version")
        return value


class HelloFrame(NotificationFrameBase):
    type: Literal["hello"] = "hello"
    request_id: NotificationIdentifier = Field(default_factory=lambda: uuid4().hex)
    token: NotificationToken = Field(repr=False)
    client_id: NotificationClientIdentifier
    subscriptions: tuple[NotificationTopicName, ...] = Field(
        default_factory=tuple,
        max_length=NOTIFICATION_MAX_SUBSCRIPTIONS,
    )


class ReadyFrame(NotificationFrameBase):
    type: Literal["ready"] = "ready"
    request_id: NotificationIdentifier
    server_id: NotificationIdentifier
    pid: int = Field(gt=0, strict=True)


class PublishFrame(NotificationFrameBase):
    type: Literal["publish"] = "publish"
    request_id: NotificationIdentifier = Field(default_factory=lambda: uuid4().hex)
    notification: Notification


class PublishedFrame(NotificationFrameBase):
    type: Literal["published"] = "published"
    request_id: NotificationIdentifier
    notification_id: NotificationIdentifier
    recipient_count: int = Field(ge=0, strict=True)


class EventFrame(NotificationFrameBase):
    type: Literal["event"] = "event"
    notification: Notification


class PingFrame(NotificationFrameBase):
    type: Literal["ping"] = "ping"
    request_id: NotificationIdentifier = Field(default_factory=lambda: uuid4().hex)


class PongFrame(NotificationFrameBase):
    type: Literal["pong"] = "pong"
    request_id: NotificationIdentifier


class ErrorFrame(NotificationFrameBase):
    type: Literal["error"] = "error"
    code: NotificationMachineCode
    request_id: NotificationIdentifier | None = None


type NotificationFrame = Annotated[
    HelloFrame | ReadyFrame | PublishFrame | PublishedFrame | EventFrame | PingFrame | PongFrame | ErrorFrame,
    Field(discriminator="type"),
]

_NOTIFICATION_FRAME_ADAPTER = TypeAdapter(NotificationFrame)


def _parse_finite_json_float(value: str) -> float:
    parsed_value = float(value)
    if not math.isfinite(parsed_value):
        raise ValueError("non-finite JSON number")
    return parsed_value


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON number")


def encode_frame(frame: NotificationFrame) -> bytes:
    """Encode one protocol frame as compact UTF-8 JSON terminated by LF."""
    try:
        content = json.dumps(
            frame.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NotificationProtocolError("invalid_message") from None

    encoded_frame = content + b"\n"
    if len(encoded_frame) > NOTIFICATION_MAX_FRAME_BYTES:
        raise NotificationProtocolError("frame_too_large")
    return encoded_frame


def decode_frame(data: bytes) -> NotificationFrame:
    """Decode exactly one bounded UTF-8 JSON frame without exposing its input."""
    if type(data) is not bytes:
        raise NotificationProtocolError("invalid_message")
    if len(data) > NOTIFICATION_MAX_FRAME_BYTES:
        raise NotificationProtocolError("frame_too_large")
    if not data.endswith(b"\n"):
        raise NotificationProtocolError("invalid_message")

    content = data[:-1]
    if not content or b"\n" in content or b"\r" in content:
        raise NotificationProtocolError("invalid_message")

    try:
        parsed_json = json.loads(
            content.decode("utf-8"),
            parse_float=_parse_finite_json_float,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(parsed_json, dict) or "version" not in parsed_json:
            raise NotificationProtocolError("invalid_message")
        if parsed_json.get("type") in ("hello", "publish", "ping") and "request_id" not in parsed_json:
            raise NotificationProtocolError("invalid_message")
        return _NOTIFICATION_FRAME_ADAPTER.validate_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NotificationProtocolError("invalid_message") from None


async def read_frame(reader: asyncio.StreamReader) -> NotificationFrame:
    """Read and decode one LF-delimited frame from an asyncio stream."""
    try:
        data = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise EOFError from None
        raise NotificationProtocolError("invalid_message") from None
    except asyncio.LimitOverrunError:
        raise NotificationProtocolError("frame_too_large") from None
    return decode_frame(data)


async def write_frame(writer: asyncio.StreamWriter, frame: NotificationFrame) -> None:
    """Write one encoded frame and bound the stream drain duration."""
    writer.write(encode_frame(frame))
    try:
        await asyncio.wait_for(writer.drain(), timeout=NOTIFICATION_IO_TIMEOUT_SECONDS)
    except TimeoutError:
        raise NotificationProtocolError("server_busy") from None


__all__ = [
    "ErrorFrame",
    "EventFrame",
    "HelloFrame",
    "Notification",
    "NotificationClientIdentifier",
    "NotificationFrame",
    "NotificationFrameBase",
    "NotificationIdentifier",
    "NotificationMachineCode",
    "NotificationProtocolError",
    "NotificationProtocolModel",
    "NotificationResource",
    "NotificationScope",
    "NotificationToken",
    "NotificationTopic",
    "NotificationTopicName",
    "PingFrame",
    "PongFrame",
    "PublishedFrame",
    "PublishFrame",
    "ReadyFrame",
    "decode_frame",
    "encode_frame",
    "read_frame",
    "write_frame",
]
