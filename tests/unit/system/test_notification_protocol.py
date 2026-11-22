import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.core.constants import NOTIFICATION_MAX_FRAME_BYTES
from app.core.notifications.protocol import (
    ErrorFrame,
    EventFrame,
    HelloFrame,
    Notification,
    NotificationProtocolError,
    NotificationResource,
    NotificationScope,
    PingFrame,
    PongFrame,
    PublishedFrame,
    PublishFrame,
    ReadyFrame,
    decode_frame,
    encode_frame,
    read_frame,
)


def _notification(**overrides):
    fields = {
        "topic": "session.reply",
        "event": "completed",
        "source": "worker:alpha",
        "occurred_at": datetime(2026, 11, 22, 1, 2, 3, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        "available_at": datetime(2026, 11, 22, 4, 5, 6, tzinfo=timezone(timedelta(hours=-4))),
        "scope": NotificationScope(
            uid="user-21",
            session_id="session-34",
            profile_id=17,
            worker_id="worker-5",
        ),
        "resource": NotificationResource(type="task.background", id="1842"),
        "correlation_id": "correlation-56",
        "data": {"nested": {"attempt": 2, "items": [True, None, {"status": "ready"}]}},
    }
    fields.update(overrides)
    return Notification(**fields)


def _valid_publish_payload():
    frame = PublishFrame(request_id="request-1", notification=_notification())
    return json.loads(encode_frame(frame))


def _json_line(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"


def _assert_protocol_error(data, code="invalid_message"):
    with pytest.raises(NotificationProtocolError) as error:
        decode_frame(data)

    assert error.value.code == code
    assert str(error.value) == code


def _assert_encode_protocol_error(frame, code):
    with pytest.raises(NotificationProtocolError) as error:
        encode_frame(frame)

    assert error.value.code == code
    assert str(error.value) == code


def test_publish_and_event_round_trip_all_notification_fields_and_resource_ids():
    resources = [
        NotificationResource(type="task.background", id="1842"),
        NotificationResource(type="terminal.command", id="9037"),
    ]

    for resource in resources:
        notification = _notification(resource=resource)
        frames = [
            PublishFrame(request_id="publish-request", notification=notification),
            EventFrame(notification=notification),
        ]

        for frame in frames:
            decoded = decode_frame(encode_frame(frame))

            assert decoded == frame
            assert decoded.notification.scope.uid == "user-21"
            assert decoded.notification.scope.session_id == "session-34"
            assert decoded.notification.scope.profile_id == 17
            assert decoded.notification.scope.worker_id == "worker-5"
            assert decoded.notification.resource.id == resource.id
            assert decoded.notification.occurred_at == notification.occurred_at
            assert decoded.notification.occurred_at.utcoffset() == timedelta(hours=5, minutes=30)
            assert decoded.notification.available_at == notification.available_at
            assert decoded.notification.available_at.utcoffset() == timedelta(hours=-4)
            assert decoded.notification.correlation_id == "correlation-56"
            assert decoded.notification.data == notification.data


@pytest.mark.parametrize(
    "topic",
    [
        "session.reply",
        "task.background",
        "task.scheduled",
        "message_platform.config",
        "message_platform.outbox",
        "memory.job",
        "knowledge.job",
        "terminal.session",
        "terminal.command",
        "session.event",
        "system.settings",
        "profile.config",
        "future.custom_event",
    ],
)
def test_established_and_future_topics_round_trip(topic):
    frame = PublishFrame(
        request_id="topic-request",
        notification=_notification(topic=topic),
    )

    decoded = decode_frame(encode_frame(frame))

    assert decoded == frame
    assert decoded.notification.topic == topic


def test_request_and_reply_frames_preserve_request_ids():
    publish = PublishFrame(request_id="publish-req-82", notification=_notification())
    published = PublishedFrame(
        request_id=publish.request_id,
        notification_id=publish.notification.notification_id,
        recipient_count=3,
    )
    ping = PingFrame(request_id="ping-req-19")
    pong = PongFrame(request_id=ping.request_id)

    decoded_publish = decode_frame(encode_frame(publish))
    decoded_published = decode_frame(encode_frame(published))
    decoded_ping = decode_frame(encode_frame(ping))
    decoded_pong = decode_frame(encode_frame(pong))

    assert decoded_publish.request_id == "publish-req-82"
    assert decoded_published.request_id == decoded_publish.request_id
    assert decoded_ping.request_id == "ping-req-19"
    assert decoded_pong.request_id == decoded_ping.request_id


def test_control_frames_round_trip_and_hello_repr_hides_random_token():
    token = secrets.token_hex(32)
    hello = HelloFrame(
        token=token,
        client_id="worker-5",
        subscriptions=("session.reply", "future.custom_event"),
    )
    frames = [
        hello,
        ReadyFrame(request_id=hello.request_id, server_id="server-8", pid=321),
        PublishedFrame(request_id="publish-request", notification_id="notification-4", recipient_count=0),
        PingFrame(request_id="ping-request"),
        PongFrame(request_id="pong-request"),
        ErrorFrame(code="not_authorized", request_id="failed-request"),
    ]

    assert token not in repr(hello)

    for frame in frames:
        assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize("version", [True, 1.0, 1.5, 2, "1", None])
def test_decode_rejects_non_integer_or_unsupported_versions(version):
    payload = _valid_publish_payload()
    payload["version"] = version

    _assert_protocol_error(_json_line(payload))


def test_decode_rejects_invalid_topics():
    payload = _valid_publish_payload()
    payload["notification"]["topic"] = "Invalid.Topic"

    _assert_protocol_error(_json_line(payload))


@pytest.mark.parametrize("extra_location", ["frame", "notification"])
def test_decode_rejects_extra_fields(extra_location):
    payload = _valid_publish_payload()
    if extra_location == "frame":
        payload["unexpected"] = "not allowed"
    else:
        payload["notification"]["unexpected"] = "not allowed"

    _assert_protocol_error(_json_line(payload))


@pytest.mark.parametrize("profile_id", [True, 0])
def test_decode_rejects_boolean_or_zero_profile_ids(profile_id):
    payload = _valid_publish_payload()
    payload["notification"]["scope"]["profile_id"] = profile_id

    _assert_protocol_error(_json_line(payload))


@pytest.mark.parametrize("field", ["occurred_at", "available_at"])
def test_decode_rejects_datetimes_without_timezone(field):
    payload = _valid_publish_payload()
    payload["notification"][field] = "2026-11-22T04:05:06"

    _assert_protocol_error(_json_line(payload))


def test_decode_rejects_invalid_utf8():
    _assert_protocol_error(b'{"version":1,"type":"ping","request_id":"x\xff"}\n')


def test_decode_rejects_isolated_unicode_surrogate():
    payload = _valid_publish_payload()
    payload["notification"]["data"] = {"invalid": "\ud800"}

    _assert_protocol_error(_json_line(payload))


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_decode_rejects_non_finite_json_numbers(number):
    payload = _valid_publish_payload()
    payload["notification"]["data"] = {"value": "__number__"}
    raw_frame = _json_line(payload).replace(b'"__number__"', number.encode("ascii"))

    _assert_protocol_error(raw_frame)


@pytest.mark.parametrize("framing", ["missing_lf", "multiple_json", "multiple_lines", "empty_line"])
def test_decode_rejects_invalid_line_framing(framing):
    frame = encode_frame(PingFrame(request_id="framing-request"))
    if framing == "missing_lf":
        raw_frame = frame[:-1]
    elif framing == "multiple_json":
        raw_frame = frame[:-1] + frame
    elif framing == "multiple_lines":
        raw_frame = frame + frame
    else:
        raw_frame = b"\n"

    _assert_protocol_error(raw_frame)


def test_escaped_newline_in_data_round_trips_as_one_frame():
    frame = PublishFrame(
        request_id="escaped-newline-request",
        notification=_notification(data={"message": "first line\nsecond line"}),
    )

    encoded = encode_frame(frame)

    assert b"first line\\nsecond line" in encoded
    assert encoded.count(b"\n") == 1
    assert decode_frame(encoded) == frame


def test_frame_size_limit_counts_utf8_bytes_and_final_lf():
    base_frame = PublishFrame(
        request_id="max-frame-request",
        notification=_notification(data={"padding": ""}),
    )
    base_size = len(encode_frame(base_frame))
    available_padding_bytes = NOTIFICATION_MAX_FRAME_BYTES - base_size
    exact_frame = PublishFrame(
        request_id="max-frame-request",
        notification=_notification(data={"padding": "x" * available_padding_bytes}),
    )
    exact_encoded = encode_frame(exact_frame)

    assert len(exact_encoded) == NOTIFICATION_MAX_FRAME_BYTES
    assert decode_frame(exact_encoded) == exact_frame

    oversized_frame = PublishFrame(
        request_id="max-frame-request",
        notification=_notification(data={"padding": "x" * (available_padding_bytes + 1)}),
    )
    _assert_encode_protocol_error(oversized_frame, "frame_too_large")
    _assert_protocol_error(exact_encoded[:-1] + b"x\n", "frame_too_large")


def test_chinese_frame_boundary_uses_utf8_byte_length():
    base_frame = PublishFrame(
        request_id="chinese-frame-request",
        notification=_notification(data={"padding": ""}),
    )
    base_size = len(encode_frame(base_frame))
    available_padding_bytes = NOTIFICATION_MAX_FRAME_BYTES - base_size
    chinese_count = available_padding_bytes // len("中".encode())
    accepted_frame = PublishFrame(
        request_id="chinese-frame-request",
        notification=_notification(data={"padding": "中" * chinese_count}),
    )
    accepted_encoded = encode_frame(accepted_frame)

    assert len(accepted_encoded) == base_size + chinese_count * 3
    assert len(accepted_encoded) <= NOTIFICATION_MAX_FRAME_BYTES
    assert len("中" * chinese_count) < len(accepted_encoded)
    assert decode_frame(accepted_encoded) == accepted_frame

    oversized_frame = PublishFrame(
        request_id="chinese-frame-request",
        notification=_notification(data={"padding": "中" * (chinese_count + 1)}),
    )
    _assert_encode_protocol_error(oversized_frame, "frame_too_large")


@pytest.mark.asyncio
async def test_read_frame_accepts_data_fed_in_segments():
    frame = PublishFrame(request_id="segmented-request", notification=_notification())
    encoded = encode_frame(frame)
    reader = asyncio.StreamReader(limit=NOTIFICATION_MAX_FRAME_BYTES)
    read_task = asyncio.create_task(read_frame(reader))

    for chunk in (encoded[:5], encoded[5:19], encoded[19:]):
        reader.feed_data(chunk)
        await asyncio.sleep(0)
    reader.feed_eof()

    assert await read_task == frame


@pytest.mark.asyncio
async def test_read_frame_reads_concatenated_frames_in_order():
    first = PingFrame(request_id="first-request")
    second = PongFrame(request_id="second-request")
    reader = asyncio.StreamReader(limit=NOTIFICATION_MAX_FRAME_BYTES)
    reader.feed_data(encode_frame(first) + encode_frame(second))
    reader.feed_eof()

    assert await read_frame(reader) == first
    assert await read_frame(reader) == second


@pytest.mark.asyncio
async def test_read_frame_raises_eof_error_when_stream_ends_between_frames():
    reader = asyncio.StreamReader(limit=NOTIFICATION_MAX_FRAME_BYTES)
    reader.feed_eof()

    with pytest.raises(EOFError):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_read_frame_rejects_incomplete_frame_at_eof():
    reader = asyncio.StreamReader(limit=NOTIFICATION_MAX_FRAME_BYTES)
    reader.feed_data(b'{"version":1,"type":"ping"')
    reader.feed_eof()

    with pytest.raises(NotificationProtocolError) as error:
        await read_frame(reader)

    assert error.value.code == "invalid_message"
    assert str(error.value) == "invalid_message"


@pytest.mark.asyncio
async def test_read_frame_rejects_input_over_stream_limit():
    reader = asyncio.StreamReader(limit=NOTIFICATION_MAX_FRAME_BYTES)
    reader.feed_data(b"x" * (NOTIFICATION_MAX_FRAME_BYTES + 1))

    with pytest.raises(NotificationProtocolError) as error:
        await read_frame(reader)

    assert error.value.code == "frame_too_large"
    assert str(error.value) == "frame_too_large"


@pytest.mark.parametrize("frame_type", ["hello", "publish", "ping"])
@pytest.mark.parametrize("missing_field", ["version", "request_id"])
def test_decode_rejects_missing_required_request_frame_fields(frame_type, missing_field):
    if frame_type == "hello":
        frame = HelloFrame(token=secrets.token_hex(32), client_id="worker-5")
    elif frame_type == "publish":
        frame = PublishFrame(notification=_notification())
    else:
        frame = PingFrame()

    payload = json.loads(encode_frame(frame))
    del payload[missing_field]

    _assert_protocol_error(_json_line(payload))


def test_event_frame_rejects_missing_version_and_round_trips_without_request_id():
    frame = EventFrame(notification=_notification())
    encoded = encode_frame(frame)
    payload = json.loads(encoded)

    assert "request_id" not in payload
    assert decode_frame(encoded) == frame

    del payload["version"]
    _assert_protocol_error(_json_line(payload))
