from collections.abc import Iterator
from dataclasses import dataclass

from app.core.constants import ERR_VALIDATION_FAILED, ERR_VALUE_MUST_BE_POSITIVE
from app.core.i18n import t
from app.models.message import InternalMessage


@dataclass(frozen=True)
class UserInputBatch:
    """Logical user input together with every persisted message it represents."""

    messages: tuple[InternalMessage, ...]
    source_message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "source_message_ids", tuple(self.source_message_ids))
        if not self.messages:
            raise ValueError(t(ERR_VALIDATION_FAILED))
        if not self.source_message_ids:
            raise ValueError(t(ERR_VALIDATION_FAILED))
        if any(not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 for message_id in self.source_message_ids):
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="source_message_ids"))

    @property
    def summary_boundary_message_id(self) -> int:
        return min(self.source_message_ids)

    @property
    def latest_message_id(self) -> int:
        return max(self.source_message_ids)

    def __iter__(self) -> Iterator[InternalMessage]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int | slice) -> InternalMessage | tuple[InternalMessage, ...]:
        return self.messages[index]
