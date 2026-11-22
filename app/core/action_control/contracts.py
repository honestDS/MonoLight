"""系统操作契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.core.constants import ERR_SYSTEM_ACTION_UNKNOWN
from app.core.i18n import t


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """系统操作定义。"""

    action_id: str
    permission: str
    prepare: Callable[[dict[str, Any]], dict[str, Any]]
    execute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    description_key: str
    confirm_with_profile: bool = True


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """已准备的系统操作。"""

    action_id: str
    parameters: dict[str, Any]
    target: dict[str, Any]
    actor_uid: str

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""

        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PreparedAction:
        """从字典恢复操作。"""

        fields = {"action_id", "parameters", "target", "actor_uid"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(t(ERR_SYSTEM_ACTION_UNKNOWN))

        action_id = value["action_id"]
        parameters = value["parameters"]
        target = value["target"]
        actor_uid = value["actor_uid"]
        if not isinstance(action_id, str) or not isinstance(parameters, dict) or not isinstance(target, dict) or not isinstance(actor_uid, str) or not action_id or not actor_uid:
            raise ValueError(t(ERR_SYSTEM_ACTION_UNKNOWN))

        return cls(**value)
