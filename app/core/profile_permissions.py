import copy

from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    PROFILE_CONFIG_SUPERUSER_ONLY_PATHS,
)
from app.core.exceptions import ForbiddenException
from app.models.profile import ProfileConfig

_MISSING = object()


def _get_path(data: dict, path: str):
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return _MISSING
        data = data[part]
    return data


def _set_path(data: dict, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


def _has_changed_leaf(submitted, baseline) -> bool:
    if submitted is _MISSING:
        return False
    if isinstance(submitted, dict):
        baseline_dict = baseline if isinstance(baseline, dict) else {}
        return any(_has_changed_leaf(value, baseline_dict.get(key, _MISSING)) for key, value in submitted.items())
    return submitted != baseline


def get_profile_config_permissions(current_user) -> dict[str, bool]:
    return {path: getattr(current_user, "is_superuser", False) is True for path in PROFILE_CONFIG_SUPERUSER_ONLY_PATHS}


def prepare_profile_configs(
    configs: dict,
    current_user,
    *,
    existing_configs: dict | None = None,
) -> dict:
    submitted_model = ProfileConfig.model_validate(copy.deepcopy(configs))
    submitted = submitted_model.model_dump(exclude_unset=True)
    normalized = submitted_model.model_dump()

    baseline_model = ProfileConfig.model_validate(copy.deepcopy(existing_configs)) if existing_configs is not None else ProfileConfig.model_validate({})
    baseline = baseline_model.model_dump()

    for path, is_allowed in get_profile_config_permissions(current_user).items():
        if is_allowed:
            continue
        submitted_value = _get_path(submitted, path)
        baseline_value = _get_path(baseline, path)
        if _has_changed_leaf(submitted_value, baseline_value):
            raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
        _set_path(normalized, path, copy.deepcopy(baseline_value))

    return ProfileConfig.model_validate(normalized).model_dump()
