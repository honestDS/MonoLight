import pytest
from pydantic import ValidationError

from app.api.v1.chat import NewSessionProfileSetting, SessionSettingRequest
from app.core.constants import SESSION_MAX_TURNS_UPPER_BOUND
from app.models.message import ChatCompletionRequest
from app.models.profile import ProfileConfig, ProfileCreate, ProfileUpdate
from app.models.session import ChatSession

REQUEST_MODELS = [
    pytest.param(ChatCompletionRequest, {"message": "hello"}, id="chat-completion"),
    pytest.param(NewSessionProfileSetting, {}, id="new-session-profile"),
    pytest.param(SessionSettingRequest, {"session_id": "s"}, id="session-setting"),
]


def test_chat_session_defaults_to_goal_mode_and_max_turns() -> None:
    session = ChatSession(session_id="s", uid="u")

    assert session.goal_mode is True
    assert session.max_turns == 5


@pytest.mark.parametrize(
    ("model", "payload"),
    REQUEST_MODELS[:2],
)
def test_new_session_requests_default_to_goal_mode_and_max_turns(model: type, payload: dict[str, object]) -> None:
    request = model.model_validate(payload)

    assert request.goal_mode is True
    assert request.max_turns == 5


def test_session_setting_request_omits_partial_update_fields_as_none() -> None:
    request = SessionSettingRequest(session_id="s")

    assert request.goal_mode is None
    assert request.max_turns is None


@pytest.mark.parametrize("max_turns", [1, 20, 21, 1_000_000, SESSION_MAX_TURNS_UPPER_BOUND])
@pytest.mark.parametrize(("model", "payload"), REQUEST_MODELS)
def test_session_requests_accept_valid_max_turns(model: type, payload: dict[str, object], max_turns: int) -> None:
    request = model.model_validate({**payload, "max_turns": max_turns})

    assert request.max_turns == max_turns


@pytest.mark.parametrize(
    "max_turns",
    [0, -1, 1.5, True, "2", None, SESSION_MAX_TURNS_UPPER_BOUND + 1, 2**63, 10**100],
)
@pytest.mark.parametrize(("model", "payload"), REQUEST_MODELS)
def test_session_requests_reject_invalid_max_turns(model: type, payload: dict[str, object], max_turns: object) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "max_turns": max_turns})


def test_chat_session_accepts_max_turns_upper_bound() -> None:
    session = ChatSession.model_validate({"session_id": "s", "uid": "u", "max_turns": SESSION_MAX_TURNS_UPPER_BOUND})

    assert session.max_turns == SESSION_MAX_TURNS_UPPER_BOUND


@pytest.mark.parametrize("max_turns", [SESSION_MAX_TURNS_UPPER_BOUND + 1, 2**63, 10**100])
def test_chat_session_rejects_max_turns_above_upper_bound(max_turns: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatSession.model_validate({"session_id": "s", "uid": "u", "max_turns": max_turns})

    assert exc_info.value.errors()[0]["input"] == max_turns


@pytest.mark.parametrize("goal_mode", [True, False])
@pytest.mark.parametrize(("model", "payload"), REQUEST_MODELS)
def test_session_requests_accept_boolean_goal_mode(model: type, payload: dict[str, object], goal_mode: bool) -> None:
    request = model.model_validate({**payload, "goal_mode": goal_mode})

    assert request.goal_mode is goal_mode


@pytest.mark.parametrize("goal_mode", [None, 0, 1, "true", [], {}])
@pytest.mark.parametrize(("model", "payload"), REQUEST_MODELS)
def test_session_requests_reject_non_boolean_goal_mode(model: type, payload: dict[str, object], goal_mode: object) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "goal_mode": goal_mode})


def _legacy_profile_configs(layout: str) -> dict[str, object]:
    values = {"goal_mode": True, "max_turns": 7, "max_parallel_tools": 3}
    return {"tool": values} if layout == "nested" else values


def _assert_legacy_session_fields_are_dropped(configs: dict[str, object]) -> None:
    tool = configs["tool"]

    assert tool["max_parallel_tools"] == 3
    assert "goal_mode" not in tool
    assert "max_turns" not in tool


@pytest.mark.parametrize("layout", ["nested", "flat"])
def test_profile_config_drops_legacy_session_fields(layout: str) -> None:
    dumped = ProfileConfig.model_validate(_legacy_profile_configs(layout)).model_dump()

    _assert_legacy_session_fields_are_dropped(dumped)


@pytest.mark.parametrize("layout", ["nested", "flat"])
@pytest.mark.parametrize("model", [ProfileCreate, ProfileUpdate])
def test_profile_write_models_drop_legacy_session_fields(model: type, layout: str) -> None:
    payload: dict[str, object] = {"configs": _legacy_profile_configs(layout)}
    if model is ProfileCreate:
        payload["name"] = "legacy-profile"

    dumped = model.model_validate(payload).model_dump()["configs"]

    _assert_legacy_session_fields_are_dropped(dumped)
