import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel.model_protection import finalize_pending_channel_model_deletions_for_organization_job
from app.core.constants import ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS, ERR_CHANNEL_MODEL_LOCKED
from app.core.i18n import t
from app.core.memory.organization import (
    build_organization_snapshot_items,
    calculate_organization_required_input_tokens,
)
from app.models.channel import normalize_channel_model_ids
from tests.integration.memory import test_memory_channel_protection as channel_protection

LongTermMemoryMutationStatus = channel_protection.LongTermMemoryMutationStatus
ParameterException = channel_protection.ParameterException
Profile = channel_protection.Profile
_assert_only_channel_lock_writes = channel_protection._assert_only_channel_lock_writes
_assert_update_impact = channel_protection._assert_update_impact
_capture_writes = channel_protection._capture_writes
_config_impact_token = channel_protection._config_impact_token
_create_active_memory_record = channel_protection._create_active_memory_record
_create_channel = channel_protection._create_channel
_create_organization_job = channel_protection._create_organization_job
_create_store = channel_protection._create_store
_embedding_model = channel_protection._embedding_model
_organization_chat_model = channel_protection._organization_chat_model
_patch_profile_sync_noops = channel_protection._patch_profile_sync_noops
channel_crud = channel_protection.channel_crud
channels = channel_protection.channels
encryption_key = channel_protection.encryption_key
list_memory_channel_references = channel_protection.list_memory_channel_references
memory_job_crud = channel_protection.memory_job_crud
memory_store_crud = channel_protection.memory_store_crud
db_session = channel_protection.db_session


@pytest.mark.asyncio
async def test_update_channel_does_not_sync_ambiguous_many_to_one_chat_model_replacement_to_single_new_id(
    db_session: AsyncSession,
) -> None:
    old_models = [_organization_chat_model("old-a"), _organization_chat_model("old-b")]
    new_models = [_organization_chat_model("new-c")]
    channel = await _create_channel(
        db_session,
        name="organization-ambiguous-update-channel",
        model_ids=old_models,
    )
    store_a = await _create_store(
        db_session,
        uid="organization-ambiguous-update-user-a",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="old-a",
    )
    store_b = await _create_store(
        db_session,
        uid="organization-ambiguous-update-user-b",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="old-b",
    )
    profile = Profile(
        uid="organization-ambiguous-profile-user",
        name="organization-ambiguous-profile",
        configs={
            "channel": {
                "chat_channel": {
                    "rules": [{"channel_id": channel.id, "model_id": "old-a"}],
                },
            },
            "security": {
                "audit_channel_id": channel.id,
                "audit_model_id": "old-b",
            },
        },
    )
    db_session.add(profile)
    await db_session.flush()

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    assert preview.code == 200
    assert preview.data["requires_confirmation"] is True
    assert preview.data["synced_memory_organization_settings"] == 0
    assert preview.data["retained_memory_organization_settings"] == 0
    assert preview.data["disabled_memory_organization_settings"] == 2
    assert preview.data["synced_profile_rules"] == 0
    assert preview.data["removed_profile_rules"] == 1
    assert preview.data["synced_audit_refs"] == 0
    assert preview.data["cleared_audit_refs"] == 1
    await db_session.refresh(store_a)
    await db_session.refresh(store_b)
    await db_session.refresh(profile)
    assert store_a.auto_organize_enabled is True
    assert store_a.organization_channel_id == channel.id
    assert store_a.organization_model_id == "old-a"
    assert store_b.auto_organize_enabled is True
    assert store_b.organization_channel_id == channel.id
    assert store_b.organization_model_id == "old-b"
    assert profile.configs["channel"]["chat_channel"]["rules"] == [{"channel_id": channel.id, "model_id": "old-a"}]
    assert profile.configs["security"]["audit_channel_id"] == channel.id
    assert profile.configs["security"]["audit_model_id"] == "old-b"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    assert response.data["requires_confirmation"] is False
    assert response.data["synced_memory_organization_settings"] == 0
    assert response.data["retained_memory_organization_settings"] == 0
    assert response.data["disabled_memory_organization_settings"] == 2
    assert response.data["synced_profile_rules"] == 0
    assert response.data["removed_profile_rules"] == 1
    assert response.data["synced_audit_refs"] == 0
    assert response.data["cleared_audit_refs"] == 1
    await db_session.refresh(store_a)
    await db_session.refresh(store_b)
    await db_session.refresh(profile)
    assert store_a.auto_organize_enabled is False
    assert store_a.organization_channel_id is None
    assert store_a.organization_model_id is None
    assert store_b.auto_organize_enabled is False
    assert store_b.organization_channel_id is None
    assert store_b.organization_model_id is None
    assert profile.configs["channel"]["chat_channel"]["rules"] == []
    assert profile.configs["security"]["audit_channel_id"] is None
    assert profile.configs["security"]["audit_model_id"] is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"description": "updated description"}, id="description"),
        pytest.param({"image_understanding": True}, id="image-understanding"),
        pytest.param({"audio_understanding": True}, id="audio-understanding"),
        pytest.param({"video_understanding": True}, id="video-understanding"),
    ],
)
async def test_update_channel_allows_non_identity_chat_model_metadata_without_memory_confirmation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: dict,
) -> None:
    old_models = [_organization_chat_model("mutable-chat")]
    new_models = [{**old_models[0], **change}]
    channel = await _create_channel(
        db_session,
        name="organization-metadata-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-metadata-update-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="mutable-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"protocol": "OPENAI_RESPONSES"}, id="protocol"),
        pytest.param({"temperature": 1.2, "top_p": 0.4}, id="runtime-parameters"),
    ],
)
async def test_update_channel_previews_and_confirms_retained_chat_model_changes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: dict,
) -> None:
    old_models = [_organization_chat_model("mutable-chat")]
    new_models = [{**old_models[0], **change}]
    channel = await _create_channel(
        db_session,
        name="organization-retained-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-retained-update-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="mutable-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, retained=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, retained=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("delete", id="delete"),
        pytest.param("usage", id="usage-change"),
        pytest.param("rename_with_parameters", id="rename-with-parameters"),
    ],
)
async def test_update_channel_previews_and_confirms_disabling_invalid_chat_model_changes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    old_models = [_organization_chat_model("protected-chat")]
    channel = await _create_channel(
        db_session,
        name=f"organization-disabled-update-channel-{change}",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid=f"organization-disabled-update-user-{change}",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="protected-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    if change == "delete":
        new_models = []
    elif change == "usage":
        new_models = [_embedding_model("protected-chat")]
    else:
        new_models = [_organization_chat_model("renamed-chat", temperature=1.2)]

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "protected-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_defers_chat_model_delete_until_organization_job_finishes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "organization-deferred-delete-chat"
    active_uid = "organization-deferred-delete-active-user"
    other_uid = "organization-deferred-delete-other-user"
    old_models = [_organization_chat_model(model_id)]
    channel = await _create_channel(
        db_session,
        name="organization-deferred-delete-channel",
        model_ids=old_models,
    )
    active_store = await _create_store(
        db_session,
        uid=active_uid,
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    other_store = await _create_store(
        db_session,
        uid=other_uid,
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    job = await _create_organization_job(
        db_session,
        uid=active_uid,
        dedupe_key="organization-deferred-delete",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id=model_id,
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=[]),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(
        preview,
        requires_confirmation=True,
        disabled=1,
        deferred=1,
        pending_deletion_models=1,
    )
    await db_session.refresh(active_store)
    await db_session.refresh(other_store)
    await db_session.refresh(job)
    assert active_store.auto_organize_enabled is True
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == model_id
    assert other_store.auto_organize_enabled is True
    assert other_store.organization_channel_id == channel.id
    assert other_store.organization_model_id == model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=[],
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(
        response,
        requires_confirmation=False,
        disabled=1,
        deferred=1,
        pending_deletion_models=1,
    )
    await db_session.refresh(active_store)
    await db_session.refresh(other_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == model_id
    assert other_store.auto_organize_enabled is False
    assert other_store.organization_channel_id is None
    assert other_store.organization_model_id is None
    pending = await channel_crud.get(db_session, channel.id)
    assert pending is not None
    assert pending.model_ids[0]["model_id"] == model_id
    assert pending.model_ids[0]["lifecycle_status"] == "pending_delete"
    assert pending.model_ids[0]["is_enabled"] is False

    completed_job = await memory_job_crud.update_status(
        db_session,
        uid=active_uid,
        job_id=job.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        commit=False,
        clear_active_mutation_key=True,
    )
    assert completed_job is not None
    deleted_count = await finalize_pending_channel_model_deletions_for_organization_job(
        db_session,
        job=completed_job,
    )
    await db_session.commit()

    assert deleted_count == 1
    finalized = await channel_crud.get(db_session, channel.id)
    assert finalized is not None
    assert finalized.model_ids == []
    await db_session.refresh(active_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id is None
    assert active_store.organization_model_id is None


@pytest.mark.asyncio
async def test_active_organization_job_exact_chat_model_rename_preserves_and_syncs_business_configs(
    db_session: AsyncSession,
) -> None:
    old_model_id = "organization-active-rename-old"
    new_model_id = "organization-active-rename-new"
    uid = "organization-active-rename-user"
    old_models = [_organization_chat_model(old_model_id)]
    new_models = [{**old_models[0], "model_id": new_model_id}]
    channel = await _create_channel(
        db_session,
        name="organization-active-rename-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid=uid,
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=old_model_id,
    )
    job = await _create_organization_job(
        db_session,
        uid=uid,
        dedupe_key="organization-active-rename",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id=old_model_id,
    )
    profile = Profile(
        uid="organization-active-rename-profile-user",
        name="organization-active-rename-profile",
        configs={
            "channel": {
                "chat_channel": {
                    "rules": [{"channel_id": channel.id, "model_id": old_model_id}],
                },
            },
            "security": {
                "audit_channel_id": channel.id,
                "audit_model_id": old_model_id,
            },
        },
    )
    db_session.add(profile)
    await db_session.flush()

    preview = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )

    assert preview.code == 200
    assert preview.data["requires_confirmation"] is True
    assert preview.data["synced_memory_organization_settings"] == 1
    assert preview.data["deferred_memory_organization_settings"] == 0
    assert preview.data["disabled_memory_organization_settings"] == 0
    assert preview.data["pending_deletion_models"] == 1
    assert preview.data["synced_profile_rules"] == 1
    assert preview.data["removed_profile_rules"] == 0
    assert preview.data["synced_audit_refs"] == 1
    assert preview.data["cleared_audit_refs"] == 0
    await db_session.refresh(store)
    await db_session.refresh(profile)
    await db_session.refresh(job)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == old_model_id
    assert profile.configs["channel"]["chat_channel"]["rules"] == [{"channel_id": channel.id, "model_id": old_model_id}]
    assert profile.configs["security"]["audit_channel_id"] == channel.id
    assert profile.configs["security"]["audit_model_id"] == old_model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING
    assert job.payload["model_config"]["channel_id"] == channel.id
    assert job.payload["model_config"]["model_id"] == old_model_id
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    assert response.data["requires_confirmation"] is False
    assert response.data["synced_memory_organization_settings"] == 1
    assert response.data["deferred_memory_organization_settings"] == 0
    assert response.data["disabled_memory_organization_settings"] == 0
    assert response.data["pending_deletion_models"] == 1
    assert response.data["synced_profile_rules"] == 1
    assert response.data["removed_profile_rules"] == 0
    assert response.data["synced_audit_refs"] == 1
    assert response.data["cleared_audit_refs"] == 0
    await db_session.refresh(store)
    await db_session.refresh(profile)
    await db_session.refresh(job)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == new_model_id
    assert profile.configs["channel"]["chat_channel"]["rules"] == [{"channel_id": channel.id, "model_id": new_model_id}]
    assert profile.configs["security"]["audit_channel_id"] == channel.id
    assert profile.configs["security"]["audit_model_id"] == new_model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING
    assert job.payload["model_config"]["channel_id"] == channel.id
    assert job.payload["model_config"]["model_id"] == old_model_id
    pending = await channel_crud.get(db_session, channel.id)
    assert pending is not None
    assert {model["model_id"] for model in pending.model_ids} == {old_model_id, new_model_id}
    models_by_id = {model["model_id"]: model for model in pending.model_ids}
    assert models_by_id[new_model_id]["is_enabled"] is True
    assert models_by_id[old_model_id]["is_enabled"] is False
    assert models_by_id[old_model_id]["lifecycle_status"] == "pending_delete"

    completed_job = await memory_job_crud.update_status(
        db_session,
        uid=uid,
        job_id=job.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        commit=False,
        clear_active_mutation_key=True,
    )
    assert completed_job is not None
    deleted_count = await finalize_pending_channel_model_deletions_for_organization_job(
        db_session,
        job=completed_job,
    )
    await db_session.commit()

    assert deleted_count == 1
    finalized = await channel_crud.get(db_session, channel.id)
    assert finalized is not None
    assert finalized.model_ids == normalize_channel_model_ids(new_models)
    await db_session.refresh(store)
    await db_session.refresh(profile)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == new_model_id
    assert profile.configs["channel"]["chat_channel"]["rules"] == [{"channel_id": channel.id, "model_id": new_model_id}]
    assert profile.configs["security"]["audit_channel_id"] == channel.id
    assert profile.configs["security"]["audit_model_id"] == new_model_id


@pytest.mark.asyncio
async def test_pending_delete_chat_model_rejects_submitted_modification(
    db_session: AsyncSession,
) -> None:
    pending_model = _organization_chat_model(
        "locked-pending-chat",
        lifecycle_status="pending_delete",
        is_enabled=False,
    )
    channel = await _create_channel(
        db_session,
        name="locked-pending-chat-channel",
        model_ids=[pending_model],
    )

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(
                model_ids=[{**pending_model, "description": "changed description"}],
            ),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_CHANNEL_MODEL_LOCKED


@pytest.mark.asyncio
async def test_update_channel_base_url_validation_rolls_back_deferred_chat_model_delete(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "atomic-base-url-chat"
    old_models = [_organization_chat_model(model_id)]
    channel = await _create_channel(
        db_session,
        name="atomic-base-url-channel",
        model_ids=old_models,
    )
    original_base_url = channel.base_url
    store = await _create_store(
        db_session,
        uid="atomic-base-url-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    job = await _create_organization_job(
        db_session,
        uid=store.uid,
        dedupe_key="atomic-base-url-organization",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id=model_id,
    )
    _patch_profile_sync_noops(monkeypatch)
    await db_session.commit()

    preview = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            base_url=None,
            model_ids=[],
        ),
        db=db_session,
        admin={},
    )
    _assert_update_impact(
        preview,
        requires_confirmation=True,
        deferred=1,
        pending_deletion_models=1,
    )

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            base_url=None,
            model_ids=[],
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    assert response.code == 422
    assert response.message == t(ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)
    await db_session.refresh(channel)
    await db_session.refresh(store)
    await db_session.refresh(job)
    assert channel.base_url == original_base_url
    assert channel.model_ids == old_models
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING


@pytest.mark.asyncio
async def test_confirmed_chat_model_delete_does_not_overwrite_concurrent_store_selection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_model_id = "organization-delete-chat"
    fallback_model_id = "organization-fallback-chat"
    original_channel = await _create_channel(
        db_session,
        name="organization-delete-race-channel",
        model_ids=[_organization_chat_model(original_model_id)],
    )
    fallback_channel = await _create_channel(
        db_session,
        name="organization-fallback-race-channel",
        model_ids=[_organization_chat_model(fallback_model_id)],
    )
    store = await _create_store(
        db_session,
        uid="organization-delete-race-user",
        auto_organize_enabled=True,
        organization_channel_id=original_channel.id,
        organization_model_id=original_model_id,
    )
    _patch_profile_sync_noops(monkeypatch)

    preview = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(model_ids=[]),
        db=db_session,
        admin={},
    )
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)

    original_conditional_update = memory_store_crud.update_auto_organize_if_channel_and_model
    conditional_update_calls = 0

    async def switch_store_before_first_conditional_update(*args, **kwargs) -> bool:
        nonlocal conditional_update_calls
        conditional_update_calls += 1
        if conditional_update_calls == 1:
            await memory_store_crud.update_by_uid(
                db_session,
                uid=store.uid,
                auto_organize_enabled=True,
                organization_channel_id=fallback_channel.id,
                organization_model_id=fallback_model_id,
                commit=False,
            )
        return await original_conditional_update(*args, **kwargs)

    monkeypatch.setattr(
        memory_store_crud,
        "update_auto_organize_if_channel_and_model",
        switch_store_before_first_conditional_update,
    )

    response = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(
            model_ids=[],
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, synced=0, disabled=0)
    assert conditional_update_calls == 1
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == fallback_channel.id
    assert store.organization_model_id == fallback_model_id
    updated = await channel_crud.get(db_session, original_channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids([])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "old_model_ids",
        "new_model_ids",
        "concurrent_model_id",
        "expected_enabled",
        "expected_model_id",
        "expected_disabled",
        "expected_concurrently_disabled",
    ),
    [
        pytest.param(
            ["old-a", "old-b"],
            [],
            "old-b",
            False,
            None,
            1,
            1,
            id="concurrent-selection-is-disabled",
        ),
        pytest.param(
            ["old-a", "keep-b"],
            ["keep-b"],
            "keep-b",
            True,
            "keep-b",
            0,
            0,
            id="concurrent-selection-is-retained",
        ),
    ],
)
async def test_confirmed_chat_model_update_handles_concurrent_same_channel_store_selection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    old_model_ids: list[str],
    new_model_ids: list[str],
    concurrent_model_id: str,
    expected_enabled: bool,
    expected_model_id: str | None,
    expected_disabled: int,
    expected_concurrently_disabled: int,
) -> None:
    original_channel = await _create_channel(
        db_session,
        name="organization-same-channel-race-channel",
        model_ids=[_organization_chat_model(model_id) for model_id in old_model_ids],
    )
    store = await _create_store(
        db_session,
        uid="organization-same-channel-race-user",
        auto_organize_enabled=True,
        organization_channel_id=original_channel.id,
        organization_model_id="old-a",
    )
    _patch_profile_sync_noops(monkeypatch)

    new_models = [_organization_chat_model(model_id) for model_id in new_model_ids]
    preview = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)

    original_conditional_update = memory_store_crud.update_auto_organize_if_channel_and_model
    conditional_update_calls = 0

    async def switch_store_before_first_conditional_update(*args, **kwargs) -> bool:
        nonlocal conditional_update_calls
        conditional_update_calls += 1
        if conditional_update_calls == 1:
            await memory_store_crud.update_by_uid(
                db_session,
                uid=store.uid,
                auto_organize_enabled=True,
                organization_channel_id=original_channel.id,
                organization_model_id=concurrent_model_id,
                commit=False,
            )
        return await original_conditional_update(*args, **kwargs)

    monkeypatch.setattr(
        memory_store_crud,
        "update_auto_organize_if_channel_and_model",
        switch_store_before_first_conditional_update,
    )

    response = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(
        response,
        requires_confirmation=False,
        disabled=expected_disabled,
        concurrently_disabled=expected_concurrently_disabled,
    )
    assert conditional_update_calls == 1
    await db_session.refresh(store)
    assert store.auto_organize_enabled is expected_enabled
    assert store.organization_channel_id == (original_channel.id if expected_enabled else None)
    assert store.organization_model_id == expected_model_id
    updated = await channel_crud.get(db_session, original_channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_organization_budget_validation_uses_each_uid_active_count(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("budget-chat", max_tokens=4096)]
    new_models = [_organization_chat_model("budget-chat", max_tokens=255)]
    channel = await _create_channel(
        db_session,
        name="organization-budget-update-channel",
        model_ids=old_models,
    )
    active_store = await _create_store(
        db_session,
        uid="organization-budget-active-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-chat",
    )
    empty_store = await _create_store(
        db_session,
        uid="organization-budget-empty-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-chat",
    )
    await _create_active_memory_record(
        db_session,
        uid="organization-budget-active-user",
        memory_key="budget-memory",
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, retained=1, disabled=1)
    await db_session.refresh(active_store)
    await db_session.refresh(empty_store)
    assert active_store.auto_organize_enabled is True
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == "budget-chat"
    assert empty_store.auto_organize_enabled is True
    assert empty_store.organization_channel_id == channel.id
    assert empty_store.organization_model_id == "budget-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, retained=1, disabled=1)
    await db_session.refresh(active_store)
    await db_session.refresh(empty_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id is None
    assert active_store.organization_model_id is None
    assert empty_store.auto_organize_enabled is True
    assert empty_store.organization_channel_id == channel.id
    assert empty_store.organization_model_id == "budget-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_organization_budget_validation_rejects_real_input_overflow_at_boundary(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("budget-boundary-chat")]
    new_models = [_organization_chat_model("budget-boundary-chat", context_window_k=13, max_tokens=11520)]
    channel = await _create_channel(
        db_session,
        name="organization-budget-boundary-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-budget-boundary-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-boundary-chat",
    )
    records = []
    for index in range(45):
        records.append(
            await _create_active_memory_record(
                db_session,
                uid="organization-budget-boundary-user",
                memory_key=f"budget-boundary-memory-{index}",
            )
        )
    snapshot_items = build_organization_snapshot_items(records)
    required_input_tokens = calculate_organization_required_input_tokens(snapshot_items)
    assert required_input_tokens > 1224
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "budget-boundary-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_requires_context_window_for_output_safety_margin_and_input_space(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("context-budget-chat")]
    new_models = [
        _organization_chat_model(
            "context-budget-chat",
            context_window_k=1,
            max_tokens=743,
        )
    ]
    channel = await _create_channel(
        db_session,
        name="organization-context-budget-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-context-budget-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="context-budget-chat",
    )
    for index in range(3):
        await _create_active_memory_record(
            db_session,
            uid="organization-context-budget-user",
            memory_key=f"context-budget-memory-{index}",
        )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "context-budget-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            model_ids=new_models,
            confirm_config_impact=True,
            config_impact_token=_config_impact_token(preview),
        ),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_active_organization_job_rejects_chat_model_update_and_rolls_back_settings(
    db_session: AsyncSession,
) -> None:
    old_models = [_organization_chat_model("job-chat")]
    channel = await _create_channel(
        db_session,
        name="active-organization-job-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="active-organization-job-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="job-chat",
    )
    job = await _create_organization_job(
        db_session,
        uid="active-organization-job-user",
        dedupe_key="active-organization-job",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id="job-chat",
    )
    await db_session.commit()
    references = await list_memory_channel_references(db_session, channel_id=channel.id)
    reference_values = {(reference.uid, reference.channel_id, reference.model_id, reference.usage, reference.is_adaptable) for reference in references}
    assert reference_values == {
        ("active-organization-job-user", channel.id, "job-chat", "CHAT", True),
        ("active-organization-job-user", channel.id, "job-chat", "CHAT", False),
    }
    assert {reference.is_adaptable for reference in references} == {True, False}
    original_job_status = job.status
    original_job_payload = dict(job.payload)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=[]),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(
        preview,
        requires_confirmation=True,
        deferred=1,
        pending_deletion_models=1,
    )

    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "job-chat"
    await db_session.refresh(job)
    assert job.status == original_job_status
    assert job.payload == original_job_payload
    references_after = await list_memory_channel_references(db_session, channel_id=channel.id)
    assert {(reference.uid, reference.channel_id, reference.model_id, reference.usage, reference.is_adaptable) for reference in references_after} == reference_values
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == old_models
