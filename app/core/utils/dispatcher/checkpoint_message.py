import hashlib
import json
from typing import Any

from app.models.message import InternalMessage


def checkpoint_message_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class CheckpointMessagePayload(dict[str, Any]):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload)
        self._content_hash = checkpoint_message_hash(self)

    @property
    def content_hash(self) -> str:
        return self._content_hash


class CheckpointMessageSerializer:
    def __init__(self) -> None:
        self._cache: dict[int, tuple[InternalMessage, CheckpointMessagePayload]] = {}

    def serialize_groups(
        self,
        groups: tuple[list[InternalMessage], ...] | list[list[InternalMessage]],
    ) -> list[list[dict[str, Any]]]:
        previous_cache = self._cache
        current_cache: dict[int, tuple[InternalMessage, CheckpointMessagePayload]] = {}
        serialized_groups: list[list[dict[str, Any]]] = []

        for group in groups:
            serialized_group: list[dict[str, Any]] = []
            for message in group:
                cache_key = id(message)
                cached = current_cache.get(cache_key)
                if cached is None:
                    cached = previous_cache.get(cache_key)

                if cached is not None and message == cached[0]:
                    snapshot, payload = cached
                else:
                    snapshot = message.model_copy(deep=True)
                    payload = CheckpointMessagePayload(snapshot.model_dump(mode="json", exclude_none=True))

                current_cache[cache_key] = (snapshot, payload)
                serialized_group.append(payload)
            serialized_groups.append(serialized_group)

        self._cache = current_cache
        return serialized_groups
