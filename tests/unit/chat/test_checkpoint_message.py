from app.core.utils.dispatcher.checkpoint_message import CheckpointMessageSerializer
from app.models.message import InternalMessage, InternalToolCall


def _message(message_id: int, content: str) -> InternalMessage:
    return InternalMessage(
        id=message_id,
        role="user",
        content=content,
        provider_metadata={"trace": {"step": message_id}},
        tool_calls=[
            InternalToolCall(
                id=f"tool-{message_id}",
                name="lookup",
                arguments={"query": {"term": content, "limit": 1}},
                provider_metadata={"vendor": "test"},
            )
        ],
        attachments=[f"file-{message_id}.txt"],
        created_at=1793775600.0 + message_id,
    )


def test_serializer_deduplicates_messages_across_groups_and_reuses_unchanged(
    monkeypatch,
):
    serializer = CheckpointMessageSerializer()
    first = _message(1, "first")
    second = _message(2, "second")
    messages = [first, second]
    turn_messages = [second, first]
    dump_count = 0
    original_model_dump = InternalMessage.model_dump

    def counted_model_dump(self, *args, **kwargs):
        nonlocal dump_count
        dump_count += 1
        return original_model_dump(self, *args, **kwargs)

    monkeypatch.setattr(InternalMessage, "model_dump", counted_model_dump)

    saved = serializer.serialize_groups((messages, turn_messages))
    assert dump_count == 2
    assert [[payload["id"] for payload in group] for group in saved] == [
        [1, 2],
        [2, 1],
    ]
    assert [payload["content"] for payload in saved[0]] == ["first", "second"]
    assert saved[1][0]["tool_calls"][0]["arguments"]["query"]["term"] == "second"

    repeated = serializer.serialize_groups((messages, turn_messages))
    assert dump_count == 2
    assert repeated == saved
    assert [payload["content"] for payload in repeated[1]] == ["second", "first"]


def test_serializer_dumps_only_messages_appended_to_existing_context(monkeypatch):
    serializer = CheckpointMessageSerializer()
    first = _message(1, "first")
    second = _message(2, "second")
    third = _message(3, "third")
    messages = [first]
    turn_messages = [first]
    dump_count = 0
    original_model_dump = InternalMessage.model_dump

    def counted_model_dump(self, *args, **kwargs):
        nonlocal dump_count
        dump_count += 1
        return original_model_dump(self, *args, **kwargs)

    monkeypatch.setattr(InternalMessage, "model_dump", counted_model_dump)

    initial = serializer.serialize_groups((messages, turn_messages))
    assert dump_count == 1

    messages.extend([second, third])
    updated = serializer.serialize_groups((messages, turn_messages))
    assert dump_count == 3
    assert updated[0] == [initial[0][0], updated[0][1], updated[0][2]]
    assert [payload["content"] for payload in updated[0]] == [
        "first",
        "second",
        "third",
    ]
    assert [payload["id"] for payload in updated[1]] == [1]


def test_serializer_detects_in_place_changes_without_mutating_old_snapshots():
    serializer = CheckpointMessageSerializer()
    message = _message(1, "before")

    initial = serializer.serialize_groups(([message],))[0][0]
    assert initial["content"] == "before"
    assert initial["tool_calls"][0]["arguments"]["query"]["term"] == "before"
    assert initial["attachments"] == ["file-1.txt"]
    assert initial["provider_metadata"] == {"trace": {"step": 1}}

    message.content = "after content"
    content_change = serializer.serialize_groups(([message],))[0][0]
    assert content_change["content"] == "after content"
    assert content_change.content_hash != initial.content_hash
    assert initial["content"] == "before"

    message.tool_calls[0].arguments["query"]["term"] = "after arguments"
    arguments_change = serializer.serialize_groups(([message],))[0][0]
    assert arguments_change["tool_calls"][0]["arguments"]["query"]["term"] == "after arguments"
    assert arguments_change.content_hash != content_change.content_hash
    assert initial["tool_calls"][0]["arguments"]["query"]["term"] == "before"
    assert content_change["tool_calls"][0]["arguments"]["query"]["term"] == "before"

    message.attachments.append("after-attachments.txt")
    attachments_change = serializer.serialize_groups(([message],))[0][0]
    assert attachments_change["attachments"] == [
        "file-1.txt",
        "after-attachments.txt",
    ]
    assert attachments_change.content_hash != arguments_change.content_hash
    assert initial["attachments"] == ["file-1.txt"]
    assert content_change["attachments"] == ["file-1.txt"]
    assert arguments_change["attachments"] == ["file-1.txt"]

    message.provider_metadata["trace"]["step"] = 99
    metadata_change = serializer.serialize_groups(([message],))[0][0]
    assert metadata_change["provider_metadata"] == {"trace": {"step": 99}}
    assert metadata_change.content_hash != attachments_change.content_hash
    assert initial["provider_metadata"] == {"trace": {"step": 1}}
    assert content_change["provider_metadata"] == {"trace": {"step": 1}}
    assert arguments_change["provider_metadata"] == {"trace": {"step": 1}}
    assert attachments_change["provider_metadata"] == {"trace": {"step": 1}}


def test_serializer_preserves_order_duplicates_and_replaced_messages():
    serializer = CheckpointMessageSerializer()
    first = _message(1, "first")
    second = _message(2, "second")
    replacement = _message(1, "replacement")

    serializer.serialize_groups(([first, second], [second, first]))
    saved = serializer.serialize_groups(([second, replacement, second], [replacement, second, replacement]))

    assert [[payload["id"] for payload in group] for group in saved] == [
        [2, 1, 2],
        [1, 2, 1],
    ]
    assert [[payload["content"] for payload in group] for group in saved] == [
        ["second", "replacement", "second"],
        ["replacement", "second", "replacement"],
    ]
    assert saved[0][1]["tool_calls"][0]["arguments"]["query"]["term"] == "replacement"
