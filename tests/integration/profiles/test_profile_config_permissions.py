from copy import deepcopy
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.profile.permissions as profile_permissions_module
from app.models.profile import ProfileConfig, ToolConfig
from tests.integration.profiles import test_profile_management_permissions as profile_management

encryption_key = profile_management.encryption_key
api_context = profile_management.api_context

_token = profile_management._token
_create_profile = profile_management._create_profile
_update_profile = profile_management._update_profile
_profile_snapshot = profile_management._profile_snapshot
_assert_standard = profile_management._assert_standard
_assert_profile_success = profile_management._assert_profile_success


@pytest.mark.asyncio
async def test_regular_user_cannot_change_security_config_on_create_or_update(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    security_values = [
        ("audit_channel_id", channel_id),
        ("audit_model_id", "chat-model"),
        ("audit_threshold", 0),
        ("audit_depth", 1),
        ("audit_confirmation_timeout_seconds", 60),
        ("audit_report_language", "en"),
        ("protect_system_directories", False),
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        forged_token = _token("user-a", is_superuser=True)
        for index, (field, value) in enumerate(security_values):
            nested = index % 2 == 0
            configs = {"security": {field: value}} if nested else {field: value}
            before_create = await _profile_snapshot(session_factory)
            create_response = await _create_profile(
                client,
                forged_token,
                {"name": f"forbidden-security-create-{index}", "configs": configs},
            )
            _assert_standard(create_response, 403)
            assert await _profile_snapshot(session_factory) == before_create == initial_snapshot

            nested = not nested
            configs = {"security": {field: value}} if nested else {field: value}
            before_update = await _profile_snapshot(session_factory)
            update_response = await _update_profile(
                client,
                forged_token,
                target_id,
                {"configs": configs},
            )
            _assert_standard(update_response, 403)
            assert await _profile_snapshot(session_factory) == before_update == initial_snapshot

        legacy_nested = {"security": {"audit_confirmation_timeout_minutes": 1}}
        legacy_flat = {"audit_confirmation_timeout_minutes": 1}
        for endpoint, configs in (("create", legacy_nested), ("update", legacy_flat)):
            before = await _profile_snapshot(session_factory)
            if endpoint == "create":
                response = await _create_profile(
                    client,
                    forged_token,
                    {"name": "forbidden-security-create-legacy", "configs": configs},
                )
            else:
                response = await _update_profile(client, forged_token, target_id, {"configs": configs})
            _assert_standard(response, 403)
            assert await _profile_snapshot(session_factory) == before == initial_snapshot


@pytest.mark.asyncio
async def test_security_defaults_and_superuser_security_create_are_persisted(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    default_security = ProfileConfig.model_validate({}).model_dump()["security"]
    starting_snapshot = await _profile_snapshot(session_factory)
    source_profile = next(row for row in starting_snapshot if row["id"] == profile_ids["user-a"]["target"])
    channel_id = source_profile["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    non_default_security = {
        "audit_channel_id": channel_id,
        "audit_model_id": "chat-model",
        "audit_threshold": 6,
        "audit_depth": 7,
        "audit_confirmation_timeout_seconds": 420,
        "audit_report_language": "en",
        "protect_system_directories": False,
    }
    created_ids = []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token, body, expected_security in (
            (
                _token("user-a"),
                {"name": "security-default-omitted", "configs": {}},
                default_security,
            ),
            (
                _token("user-a"),
                {"name": "security-default-explicit", "configs": {"security": default_security}},
                default_security,
            ),
            (
                _token("admin"),
                {"uid": "user-b", "name": "security-admin-created", "configs": {"security": non_default_security}},
                non_default_security,
            ),
        ):
            response_data = _assert_profile_success(await _create_profile(client, token, body))
            created_ids.append(response_data["id"])
            assert response_data["configs"]["security"] == expected_security

            snapshot = await _profile_snapshot(session_factory)
            created_profile = next(row for row in snapshot if row["id"] == response_data["id"])
            assert created_profile["configs"]["security"] == expected_security
            assert response_data["configs"] == created_profile["configs"]
            assert {row["id"]: row for row in snapshot if row["id"] not in created_ids} == {row["id"]: row for row in starting_snapshot}


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "user-a"])
async def test_admin_and_regular_updates_preserve_existing_configs_for_omitted_or_equal_security(
    actor: str,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    security_values = {
        "audit_channel_id": channel_id,
        "audit_model_id": "chat-model",
        "audit_threshold": 6,
        "audit_depth": 7,
        "audit_confirmation_timeout_seconds": 420,
        "audit_report_language": "en",
        "protect_system_directories": False,
    }
    admin_configs = deepcopy(target["configs"])
    admin_configs["security"] = security_values
    admin_configs["channel"]["chat_channel"]["chat_timeout"] = 97.0
    admin_configs["channel"]["context_summary_channel"]["chat_timeout"] = 43.0
    admin_configs["tool"]["max_parallel_tools"] = 9
    admin_configs["other"]["context_summary_threshold_percent"] = 80
    admin_configs["memory"].update(
        {
            "precheck_enabled": False,
            "top_k": 8,
            "candidate_k": 20,
            "chat_history": {"top_k": 7, "candidate_k": 50},
            "knowledge": {"top_k": 6, "candidate_k": 30},
        }
    )
    expected_configs = deepcopy(admin_configs)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": admin_configs},
        )
        admin_data = _assert_profile_success(admin_response)
        assert admin_data["configs"]["security"] == security_values
        assert admin_data["configs"] == expected_configs
        after_admin = await _profile_snapshot(session_factory)
        persisted_target = next(row for row in after_admin if row["id"] == target_id)
        assert persisted_target["configs"]["security"] == security_values
        assert persisted_target["configs"] == expected_configs

        unchanged_profiles = {row["id"]: row for row in after_admin if row["id"] != target_id}
        same_security_cases = [
            ("security-omitted", {"configs": {"tool": {"tool_timeout": 31.0}}}),
            ("security-empty", {"configs": {"security": {}, "tool": {"tool_timeout": 32.0}}}),
            ("security-partial-same", {"configs": {"security": {"audit_threshold": 6}, "tool": {"tool_timeout": 33.0}}}),
            ("security-complete-same", {"configs": {"security": security_values, "tool": {"tool_timeout": 34.0}}}),
            (
                "security-legacy-timeout-same",
                {"configs": {"security": {"audit_confirmation_timeout_minutes": 7}, "tool": {"tool_timeout": 35.0}}},
            ),
            ("configs-null", {"configs": None}),
            ("configs-empty", {"configs": {}}),
            ("configs-omitted", {}),
        ]
        for case_name, body in same_security_cases:
            body = {"name": f"{actor}-{case_name}", **body}
            submitted_configs = body.get("configs")
            if isinstance(submitted_configs, dict):
                submitted_tool = submitted_configs.get("tool")
                if isinstance(submitted_tool, dict) and "tool_timeout" in submitted_tool:
                    expected_configs["tool"]["tool_timeout"] = submitted_tool["tool_timeout"]

            first_response = await _update_profile(client, _token(actor), target_id, body)
            first_data = _assert_profile_success(first_response)
            after_first = await _profile_snapshot(session_factory)
            first_target = next(row for row in after_first if row["id"] == target_id)
            assert first_target["configs"]["security"] == security_values
            assert first_data["configs"]["security"] == security_values
            assert first_target["configs"] == expected_configs
            assert first_data["configs"] == expected_configs
            assert first_target["name"] == body["name"]
            assert {row["id"]: row for row in after_first if row["id"] != target_id} == unchanged_profiles

            second_response = await _update_profile(client, _token(actor), target_id, body)
            second_data = _assert_profile_success(second_response)
            after_second = await _profile_snapshot(session_factory)
            second_target = next(row for row in after_second if row["id"] == target_id)
            assert after_second == after_first
            assert second_target["configs"]["security"] == security_values
            assert second_target["configs"] == expected_configs
            assert second_data["configs"] == expected_configs
            assert {row["id"]: row for row in after_second if row["id"] != target_id} == unchanged_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected_changes"),
    [
        pytest.param(
            {
                "security": {"audit_threshold": 0, "protect_system_directories": False},
                "memory": {"precheck_enabled": False},
                "tool": {"enabled_tools": [], "firecrawl_api_key": None},
            },
            (
                (("security", "audit_threshold"), 0),
                (("security", "protect_system_directories"), False),
                (("memory", "precheck_enabled"), False),
                (("tool", "enabled_tools"), []),
                (("tool", "firecrawl_api_key"), None),
            ),
            id="explicit-falsy-values",
        ),
        pytest.param(
            {"channel": {"chat_channel": {"chat_timeout": 19.0}}},
            ((("channel", "chat_channel", "chat_timeout"), 19.0),),
            id="single-channel-leaf",
        ),
        pytest.param(
            {
                "memory": {
                    "top_k": 25,
                    "chat_history": {"top_k": 18},
                    "knowledge": {"top_k": 24},
                }
            },
            (
                (("memory", "top_k"), 25),
                (("memory", "chat_history", "top_k"), 18),
                (("memory", "knowledge", "top_k"), 24),
            ),
            id="memory-top-k-leaves",
        ),
        pytest.param(
            {"rerank_channel": {"kb_query_top_k": 26}},
            ((("memory", "knowledge", "top_k"), 26),),
            id="legacy-rerank-top-k",
        ),
        pytest.param(
            {
                "security": {"audit_channel_id": None, "audit_model_id": None},
                "channel": {"context_summary_channel": {"rules": []}},
            },
            (
                (("security", "audit_channel_id"), None),
                (("security", "audit_model_id"), None),
                (("channel", "context_summary_channel", "rules"), []),
            ),
            id="explicit-nulls-and-empty-list",
        ),
        pytest.param(
            {
                "security": {},
                "memory": {"chat_history": {}, "knowledge": {}},
                "channel": {"chat_channel": {}, "context_summary_channel": {}},
                "tool": {},
            },
            (),
            id="nested-empty-dictionaries",
        ),
        pytest.param(
            {"audit_confirmation_timeout_minutes": 2, "memory_top_k": 27},
            (
                (("security", "audit_confirmation_timeout_seconds"), 120),
                (("memory", "top_k"), 27),
            ),
            id="legacy-flat-fields",
        ),
    ],
)
async def test_admin_partial_update_replaces_only_explicit_config_leaves(
    patch: dict[str, Any],
    expected_changes: tuple[tuple[tuple[str, ...], Any], ...],
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    before_seed = await _profile_snapshot(session_factory)
    target = next(row for row in before_seed if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]

    seed_configs = deepcopy(target["configs"])
    seed_configs["security"].update(
        {
            "audit_channel_id": channel_id,
            "audit_model_id": "chat-model",
            "audit_threshold": 6,
            "protect_system_directories": True,
        }
    )
    seed_configs["channel"]["chat_channel"]["chat_timeout"] = 97.0
    seed_configs["channel"]["context_summary_channel"]["chat_timeout"] = 43.0
    seed_configs["channel"]["context_summary_channel"]["rules"] = deepcopy(seed_configs["channel"]["chat_channel"]["rules"])
    seed_configs["tool"].update({"firecrawl_api_key": "seed-firecrawl-api-key", "enabled_tools": ["file_tool"]})
    seed_configs["memory"].update(
        {
            "precheck_enabled": True,
            "top_k": 8,
            "candidate_k": 30,
            "chat_history": {"top_k": 7, "candidate_k": 80},
            "knowledge": {"top_k": 6, "candidate_k": 31},
        }
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        seed_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": seed_configs},
        )
        seed_data = _assert_profile_success(seed_response)
        assert seed_data["configs"] == seed_configs

        after_seed = await _profile_snapshot(session_factory)
        seeded_target = next(row for row in after_seed if row["id"] == target_id)
        assert seeded_target["configs"] == seed_configs
        unchanged_profiles = {row["id"]: row for row in after_seed if row["id"] != target_id}
        assert {row["id"]: row for row in after_seed if row["id"] != target_id} == {row["id"]: row for row in before_seed if row["id"] != target_id}

        expected_configs = deepcopy(seeded_target["configs"])
        for path, value in expected_changes:
            expected_parent = expected_configs
            for key in path[:-1]:
                expected_parent = expected_parent[key]
            expected_parent[path[-1]] = deepcopy(value)

        first_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": deepcopy(patch)},
        )
        first_data = _assert_profile_success(first_response)
        after_first = await _profile_snapshot(session_factory)
        first_target = next(row for row in after_first if row["id"] == target_id)
        assert first_data["configs"] == expected_configs
        assert first_target["configs"] == expected_configs
        assert {row["id"]: row for row in after_first if row["id"] != target_id} == unchanged_profiles

        second_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": deepcopy(patch)},
        )
        second_data = _assert_profile_success(second_response)
        after_second = await _profile_snapshot(session_factory)
        second_target = next(row for row in after_second if row["id"] == target_id)
        assert second_data == first_data
        assert after_second == after_first
        assert second_data["configs"] == expected_configs
        assert second_target["configs"] == expected_configs
        assert {row["id"]: row for row in after_second if row["id"] != target_id} == unchanged_profiles


@pytest.mark.asyncio
async def test_profile_config_permissions_support_a_single_field_restriction(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, profile_ids = api_context
    monkeypatch.setattr(
        profile_permissions_module,
        "PROFILE_CONFIG_SUPERUSER_ONLY_PATHS",
        ("security", "tool.max_parallel_tools"),
    )
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    admin_configs = deepcopy(target["configs"])
    admin_configs["tool"]["max_parallel_tools"] = 9

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin_update = await _update_profile(client, _token("admin"), target_id, {"configs": admin_configs})
        admin_data = _assert_profile_success(admin_update)
        assert admin_data["configs"]["tool"]["max_parallel_tools"] == 9
        after_admin = await _profile_snapshot(session_factory)
        persisted_target = next(row for row in after_admin if row["id"] == target_id)
        assert persisted_target["configs"]["tool"]["max_parallel_tools"] == 9
        assert admin_data["configs"] == persisted_target["configs"]

        for token, expected in (
            (_token("user-a", is_superuser=True), {"security": False, "tool.max_parallel_tools": False}),
            (_token("admin"), {"security": True, "tool.max_parallel_tools": True}),
        ):
            list_response = await client.get(
                "/api/v1/profiles/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert list_response.status_code == 200
            assert list_response.json()["data"]["meta"]["config_permissions"] == expected

        allowed_response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {"configs": {"tool": {"tool_timeout": 41.0}}},
        )
        allowed_data = _assert_profile_success(allowed_response)
        after_allowed = await _profile_snapshot(session_factory)
        allowed_target = next(row for row in after_allowed if row["id"] == target_id)
        assert allowed_target["configs"]["tool"]["max_parallel_tools"] == 9
        assert allowed_target["configs"]["tool"]["tool_timeout"] == 41.0
        assert allowed_data["configs"] == allowed_target["configs"]
        assert {row["id"]: row for row in after_allowed if row["id"] != target_id} == {row["id"]: row for row in after_admin if row["id"] != target_id}

        before_forbidden = await _profile_snapshot(session_factory)
        forbidden_response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {"configs": {"tool": {"max_parallel_tools": 10, "tool_timeout": 55.0}}},
        )
        _assert_standard(forbidden_response, 403)
        assert await _profile_snapshot(session_factory) == before_forbidden


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected_location"),
    [
        pytest.param(
            {"memory": {"candidate_k": 3}},
            "configs.memory",
            id="memory-candidate-below-top-k",
        ),
        pytest.param(
            {"memory": {"chat_history": {"candidate_k": 3}}},
            "configs.memory.chat_history",
            id="chat-history-candidate-below-top-k",
        ),
        pytest.param(
            {"memory": {"knowledge": {"candidate_k": 3}}},
            "configs.memory.knowledge",
            id="knowledge-candidate-below-top-k",
        ),
        pytest.param(
            None,
            "configs.memory",
            id="embedding-channel-without-model",
        ),
        pytest.param(
            {"memory": {"embedding_model_id": "unpaired-model"}},
            "configs.memory",
            id="embedding-model-without-channel",
        ),
        pytest.param(
            {"security": {"audit_depth": True}},
            "configs.security.audit_depth",
            id="boolean-audit-depth",
        ),
    ],
)
async def test_admin_partial_update_rejects_invalid_merged_config_without_mutation(
    patch: dict[str, Any] | None,
    expected_location: str,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]

    seed_configs = deepcopy(target["configs"])
    seed_memory = seed_configs["memory"]
    seed_memory.update({"top_k": 8, "candidate_k": 20})
    seed_memory["chat_history"].update({"top_k": 7, "candidate_k": 50})
    seed_memory["knowledge"].update({"top_k": 6, "candidate_k": 30})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        seed_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": seed_configs},
        )
        seed_data = _assert_profile_success(seed_response)
        assert seed_data["configs"] == seed_configs

        before = await _profile_snapshot(session_factory)
        seeded_target = next(row for row in before if row["id"] == target_id)
        assert seeded_target["configs"] == seed_configs
        assert {row["id"]: row for row in before if row["id"] != target_id} == {row["id"]: row for row in initial_snapshot if row["id"] != target_id}

        if patch is None:
            invalid_patch = {"memory": {"embedding_channel_id": channel_id}}
        else:
            invalid_patch = deepcopy(patch)
        response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"name": "invalid-partial-update-must-not-rename", "configs": invalid_patch},
        )

    payload = _assert_standard(response, 422)
    assert expected_location in payload["message"]
    assert await _profile_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_regular_user_image_compression_config_is_persisted_across_partial_updates(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, _profile_ids = api_context
    before = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await _create_profile(
            client,
            _token("user-a"),
            {
                "name": "image-compression-persistence",
                "configs": {"tool": {"image_compression_enabled": False, "image_compression_threshold_kb": 1}},
            },
        )
        created = _assert_profile_success(create_response)
        profile_id = created["id"]
        assert created["configs"]["tool"]["image_compression_enabled"] is False
        assert created["configs"]["tool"]["image_compression_threshold_kb"] == 1

        after_create = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_create if row["id"] == profile_id)
        assert persisted["configs"] == created["configs"]
        assert persisted["configs"]["tool"]["image_compression_enabled"] is False
        assert persisted["configs"]["tool"]["image_compression_threshold_kb"] == 1
        assert tuple(row for row in after_create if row["id"] != profile_id) == before

        threshold_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"tool": {"image_compression_threshold_kb": 2048}}},
        )
        threshold_data = _assert_profile_success(threshold_response)
        assert threshold_data["configs"]["tool"]["image_compression_enabled"] is False
        assert threshold_data["configs"]["tool"]["image_compression_threshold_kb"] == 2048
        after_threshold = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_threshold if row["id"] == profile_id)
        assert persisted["configs"] == threshold_data["configs"]
        assert persisted["configs"]["tool"]["image_compression_enabled"] is False
        assert persisted["configs"]["tool"]["image_compression_threshold_kb"] == 2048
        assert tuple(row for row in after_threshold if row["id"] != profile_id) == before

        timeout_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"tool": {"tool_timeout": 31.0}}},
        )
        timeout_data = _assert_profile_success(timeout_response)
        assert timeout_data["configs"]["tool"]["image_compression_enabled"] is False
        assert timeout_data["configs"]["tool"]["image_compression_threshold_kb"] == 2048
        assert timeout_data["configs"]["tool"]["tool_timeout"] == 31.0
        after_timeout = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_timeout if row["id"] == profile_id)
        assert persisted["configs"] == timeout_data["configs"]
        assert tuple(row for row in after_timeout if row["id"] != profile_id) == before

        enable_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"tool": {"image_compression_enabled": True}}},
        )
        enable_data = _assert_profile_success(enable_response)
        assert enable_data["configs"]["tool"]["image_compression_enabled"] is True
        assert enable_data["configs"]["tool"]["image_compression_threshold_kb"] == 2048
        after_enable = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_enable if row["id"] == profile_id)
        assert persisted["configs"] == enable_data["configs"]
        assert tuple(row for row in after_enable if row["id"] != profile_id) == before

        list_response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": f"Bearer {_token('user-a')}"},
        )

    assert list_response.status_code == 200
    listed = next(item for item in list_response.json()["data"]["items"] if item["id"] == profile_id)
    assert listed["configs"]["tool"]["image_compression_enabled"] is True
    assert listed["configs"]["tool"]["image_compression_threshold_kb"] == 2048
    assert next(row for row in await _profile_snapshot(session_factory) if row["id"] == profile_id)["configs"] == listed["configs"]


@pytest.mark.asyncio
async def test_image_compression_defaults_apply_to_config_without_new_fields_and_flat_input_persists(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, _profile_ids = api_context
    before = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        defaults_response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": f"Bearer {_token('user-a')}"},
        )
        assert defaults_response.status_code == 200
        default_threshold_kb = defaults_response.json()["data"]["meta"]["config_defaults"]["tool"]["image_compression_threshold_kb"]
        assert default_threshold_kb == ToolConfig().image_compression_threshold_kb

        default_response = await _create_profile(
            client,
            _token("user-a"),
            {"name": "image-compression-defaults", "configs": {}},
        )
        default_data = _assert_profile_success(default_response)
        default_id = default_data["id"]
        assert default_data["configs"]["tool"]["image_compression_enabled"] is True
        assert default_data["configs"]["tool"]["image_compression_threshold_kb"] == default_threshold_kb
        after_default = await _profile_snapshot(session_factory)
        persisted_default = next(row for row in after_default if row["id"] == default_id)
        assert persisted_default["configs"] == default_data["configs"]
        assert persisted_default["configs"]["tool"]["image_compression_enabled"] is True
        assert persisted_default["configs"]["tool"]["image_compression_threshold_kb"] == default_threshold_kb
        assert tuple(row for row in after_default if row["id"] != default_id) == before

        flat_response = await _create_profile(
            client,
            _token("user-a"),
            {
                "name": "image-compression-flat-input",
                "configs": {"image_compression_enabled": False, "image_compression_threshold_kb": 256},
            },
        )
        flat_data = _assert_profile_success(flat_response)
        flat_id = flat_data["id"]
        assert flat_data["configs"]["tool"]["image_compression_enabled"] is False
        assert flat_data["configs"]["tool"]["image_compression_threshold_kb"] == 256
        after_flat = await _profile_snapshot(session_factory)
        persisted_flat = next(row for row in after_flat if row["id"] == flat_id)
        assert persisted_flat["configs"] == flat_data["configs"]
        assert persisted_flat["configs"]["tool"]["image_compression_enabled"] is False
        assert persisted_flat["configs"]["tool"]["image_compression_threshold_kb"] == 256
        assert tuple(row for row in after_flat if row["id"] not in {default_id, flat_id}) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("image_compression_enabled", "false", id="enabled-string-false"),
        pytest.param("image_compression_enabled", 0, id="enabled-zero"),
        pytest.param("image_compression_enabled", None, id="enabled-null"),
        pytest.param("image_compression_threshold_kb", 0, id="threshold-zero"),
        pytest.param("image_compression_threshold_kb", -1, id="threshold-negative"),
        pytest.param("image_compression_threshold_kb", True, id="threshold-boolean"),
        pytest.param("image_compression_threshold_kb", 1.5, id="threshold-float"),
        pytest.param("image_compression_threshold_kb", "500", id="threshold-string"),
        pytest.param("image_compression_threshold_kb", None, id="threshold-null"),
    ],
)
async def test_regular_user_image_compression_invalid_values_return_422_without_mutation(
    field: str,
    value: Any,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    before = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {"configs": {"tool": {field: value}}},
        )

    _assert_standard(response, 422)
    assert await _profile_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_profile_subagent_channel_create_and_update_persists_rules(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    before = await _profile_snapshot(session_factory)
    source_profile = next(row for row in before if row["id"] == profile_ids["user-a"]["target"])
    channel_id = source_profile["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    chat_rule = {
        "channel_id": channel_id,
        "model_id": "chat-model",
        "priority": 1,
        "weight": 100,
    }
    create_subagent_rule = {
        "channel_id": channel_id,
        "model_id": "chat-model",
        "priority": 2,
        "weight": 50,
    }
    create_configs = {
        "channel": {
            "chat_channel": {"rules": [chat_rule]},
            "subagent_channel": {"rules": [create_subagent_rule]},
        }
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await _create_profile(
            client,
            _token("user-a"),
            {"name": "subagent-channel-persistence", "configs": create_configs},
        )
        created_data = _assert_profile_success(create_response)
        profile_id = created_data["id"]
        created_subagent_rules = created_data["configs"]["channel"]["subagent_channel"]["rules"]
        assert len(created_subagent_rules) == 1
        assert created_subagent_rules[0]["channel_id"] == channel_id
        assert created_subagent_rules[0]["model_id"] == "chat-model"
        assert created_subagent_rules[0]["priority"] == 2
        assert created_subagent_rules[0]["weight"] == 50
        assert created_data["configs"]["channel"]["chat_channel"]["rules"][0]["priority"] == 1

        after_create = await _profile_snapshot(session_factory)
        persisted_after_create = next(row for row in after_create if row["id"] == profile_id)
        assert persisted_after_create["configs"] == created_data["configs"]
        assert persisted_after_create["configs"]["channel"]["subagent_channel"]["rules"] == created_subagent_rules
        assert tuple(row for row in after_create if row["id"] != profile_id) == before

        update_subagent_rule = {
            "channel_id": channel_id,
            "model_id": "chat-model",
            "priority": 3,
            "weight": 25,
        }
        update_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"channel": {"subagent_channel": {"rules": [update_subagent_rule]}}}},
        )
        updated_data = _assert_profile_success(update_response)
        updated_subagent_rules = updated_data["configs"]["channel"]["subagent_channel"]["rules"]
        assert len(updated_subagent_rules) == 1
        assert updated_subagent_rules[0]["channel_id"] == channel_id
        assert updated_subagent_rules[0]["model_id"] == "chat-model"
        assert updated_subagent_rules[0]["priority"] == 3
        assert updated_subagent_rules[0]["weight"] == 25
        assert updated_data["configs"]["channel"]["chat_channel"]["rules"] == created_data["configs"]["channel"]["chat_channel"]["rules"]

        after_update = await _profile_snapshot(session_factory)
        persisted_after_update = next(row for row in after_update if row["id"] == profile_id)
        assert persisted_after_update["configs"] == updated_data["configs"]
        assert persisted_after_update["configs"]["channel"]["subagent_channel"]["rules"] == updated_subagent_rules

        before_invalid_create = await _profile_snapshot(session_factory)
        invalid_channel_configs = deepcopy(create_configs)
        invalid_channel_configs["channel"]["subagent_channel"]["rules"][0]["channel_id"] = 999999
        invalid_channel_response = await _create_profile(
            client,
            _token("user-a"),
            {"name": "subagent-channel-invalid-channel", "configs": invalid_channel_configs},
        )
        _assert_standard(invalid_channel_response, 400)
        assert await _profile_snapshot(session_factory) == before_invalid_create

        before_invalid_update = await _profile_snapshot(session_factory)
        invalid_config_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"channel": {"subagent_channel": {"rules": [{"channel_id": channel_id, "model_id": "chat-model", "priority": 0, "weight": 1}]}}}},
        )
        _assert_standard(invalid_config_response, 422)
        assert await _profile_snapshot(session_factory) == before_invalid_update


@pytest.mark.asyncio
async def test_regular_user_context_summary_max_concurrency_persists_across_profile_operations(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, _profile_ids = api_context
    before = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await _create_profile(
            client,
            _token("user-a"),
            {
                "name": "context-summary-concurrency-configured",
                "configs": {"other": {"context_summary_max_concurrency": 1}},
            },
        )
        created_data = _assert_profile_success(create_response)
        profile_id = created_data["id"]
        assert created_data["configs"]["other"]["context_summary_max_concurrency"] == 1

        after_create = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_create if row["id"] == profile_id)
        assert persisted["configs"] == created_data["configs"]
        assert persisted["configs"]["other"]["context_summary_max_concurrency"] == 1
        assert tuple(row for row in after_create if row["id"] != profile_id) == before

        expected_configs = deepcopy(created_data["configs"])
        expected_configs["other"]["context_summary_max_concurrency"] = 20
        update_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"other": {"context_summary_max_concurrency": 20}}},
        )
        updated_data = _assert_profile_success(update_response)
        assert updated_data["configs"] == expected_configs

        after_update = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_update if row["id"] == profile_id)
        assert persisted["configs"] == expected_configs
        assert tuple(row for row in after_update if row["id"] != profile_id) == before

        default_response = await _create_profile(
            client,
            _token("user-a"),
            {"name": "context-summary-concurrency-default", "configs": {}},
        )
        default_data = _assert_profile_success(default_response)
        default_id = default_data["id"]
        assert default_data["configs"]["other"]["context_summary_max_concurrency"] == 4

        after_default = await _profile_snapshot(session_factory)
        persisted_default = next(row for row in after_default if row["id"] == default_id)
        assert persisted_default["configs"] == default_data["configs"]
        assert persisted_default["configs"]["other"]["context_summary_max_concurrency"] == 4
        assert next(row for row in after_default if row["id"] == profile_id)["configs"] == expected_configs
        assert tuple(row for row in after_default if row["id"] != default_id) == after_update

        before_threshold = await _profile_snapshot(session_factory)
        threshold_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"configs": {"other": {"context_summary_threshold_percent": 80}}},
        )
        threshold_data = _assert_profile_success(threshold_response)
        threshold_configs = deepcopy(expected_configs)
        threshold_configs["other"]["context_summary_threshold_percent"] = 80
        assert threshold_data["configs"] == threshold_configs
        assert threshold_data["configs"]["other"]["context_summary_max_concurrency"] == 20

        after_threshold = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_threshold if row["id"] == profile_id)
        assert persisted["configs"] == threshold_configs
        assert persisted["configs"]["other"]["context_summary_max_concurrency"] == 20
        assert tuple(row for row in after_threshold if row["id"] != profile_id) == tuple(row for row in before_threshold if row["id"] != profile_id)

        before_name = await _profile_snapshot(session_factory)
        name_response = await _update_profile(
            client,
            _token("user-a"),
            profile_id,
            {"name": "context-summary-concurrency-renamed"},
        )
        name_data = _assert_profile_success(name_response)
        assert name_data["name"] == "context-summary-concurrency-renamed"
        assert name_data["configs"] == threshold_configs
        assert name_data["configs"]["other"]["context_summary_max_concurrency"] == 20

        after_name = await _profile_snapshot(session_factory)
        persisted = next(row for row in after_name if row["id"] == profile_id)
        assert persisted["name"] == "context-summary-concurrency-renamed"
        assert persisted["configs"] == threshold_configs
        assert tuple(row for row in after_name if row["id"] != profile_id) == tuple(row for row in before_name if row["id"] != profile_id)

        list_response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": f"Bearer {_token('user-a')}"},
        )

    assert list_response.status_code == 200
    listed_items = {item["id"]: item for item in list_response.json()["data"]["items"]}
    assert listed_items[profile_id]["configs"] == threshold_configs
    assert listed_items[profile_id]["configs"]["other"]["context_summary_max_concurrency"] == 20
    assert listed_items[default_id]["configs"]["other"]["context_summary_max_concurrency"] == 4
    final_snapshot = await _profile_snapshot(session_factory)
    assert next(row for row in final_snapshot if row["id"] == profile_id)["configs"] == listed_items[profile_id]["configs"]
    assert next(row for row in final_snapshot if row["id"] == default_id)["configs"] == listed_items[default_id]["configs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(21, id="above-maximum"),
        pytest.param(True, id="boolean"),
        pytest.param(1.5, id="float"),
        pytest.param("4", id="string"),
        pytest.param(None, id="null"),
    ],
)
async def test_regular_user_context_summary_max_concurrency_invalid_values_return_422_without_mutation(
    value: Any,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_create = await _profile_snapshot(session_factory)
        create_response = await _create_profile(
            client,
            _token("user-a"),
            {
                "name": "context-summary-concurrency-invalid-create",
                "configs": {"other": {"context_summary_max_concurrency": value}},
            },
        )
        _assert_standard(create_response, 422)
        assert await _profile_snapshot(session_factory) == before_create

        before_update = await _profile_snapshot(session_factory)
        update_response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {
                "name": "context-summary-concurrency-invalid-update",
                "configs": {"other": {"context_summary_max_concurrency": value}},
            },
        )
        _assert_standard(update_response, 422)
        assert await _profile_snapshot(session_factory) == before_update
