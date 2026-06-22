from app.api.v1.channels import (
    _apply_model_id_renames_to_configs,
    _collect_channel_rule_model_ids,
    _compute_model_id_renames,
)


def test_collect_channel_rule_model_ids_reads_profile_channel_refs():
    configs = {
        "channel": {
            "chat_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "chat-a", "priority": 1, "weight": 1},
                    {"channel_id": 2, "model_id": "other-channel-model", "priority": 1, "weight": 1},
                ],
            },
            "embedding_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "embedding-a", "priority": 1, "weight": 1},
                ],
            },
        },
    }

    assert _collect_channel_rule_model_ids(configs, channel_id=1) == {
        "CHAT": {"chat-a"},
        "EMBEDDING": {"embedding-a"},
        "RERANK": set(),
    }


def test_compute_model_id_renames_uses_referenced_old_model_ids_and_matching_model_config():
    old_model_ids = [
        {"model_id": "chat-old-a", "usage": "CHAT", "temperature": 0.7, "max_tokens": 2048},
        {"model_id": "chat-old-b", "usage": "CHAT", "temperature": 0.8, "max_tokens": 1024},
        {"model_id": "embedding-old", "usage": "EMBEDDING", "embedding_dimensions": 1024},
    ]
    new_model_ids = [
        {"model_id": "chat-new-a", "usage": "CHAT", "temperature": 0.7, "max_tokens": 2048},
        {"model_id": "chat-new-b", "usage": "CHAT", "temperature": 0.8, "max_tokens": 1024},
        {"model_id": "embedding-new", "usage": "EMBEDDING", "embedding_dimensions": 1024},
    ]
    referenced_model_ids = {
        "CHAT": {"chat-old-a", "chat-old-b"},
        "EMBEDDING": {"embedding-old"},
        "RERANK": set(),
    }

    assert _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids) == {
        "CHAT": {
            "chat-old-a": "chat-new-a",
            "chat-old-b": "chat-new-b",
        },
        "EMBEDDING": {
            "embedding-old": "embedding-new",
        },
    }


def test_compute_model_id_renames_only_considers_profile_referenced_models():
    old_model_ids = [
        {"model_id": "chat-referenced", "usage": "CHAT", "temperature": 0.7},
        {"model_id": "chat-unreferenced", "usage": "CHAT", "temperature": 0.8},
    ]
    new_model_ids = [
        {"model_id": "chat-renamed", "usage": "CHAT", "temperature": 0.7},
        {"model_id": "chat-unreferenced-renamed", "usage": "CHAT", "temperature": 0.8},
    ]
    referenced_model_ids = {
        "CHAT": {"chat-referenced"},
        "EMBEDDING": set(),
        "RERANK": set(),
    }

    assert _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids) == {
        "CHAT": {"chat-referenced": "chat-renamed"},
    }


def test_compute_model_id_renames_skips_when_matching_new_model_is_not_unique():
    old_model_ids = [
        {"model_id": "chat-old", "usage": "CHAT", "temperature": 0.7},
    ]
    new_model_ids = [
        {"model_id": "chat-new-a", "usage": "CHAT", "temperature": 0.7},
        {"model_id": "chat-new-b", "usage": "CHAT", "temperature": 0.7},
    ]
    referenced_model_ids = {
        "CHAT": {"chat-old"},
        "EMBEDDING": set(),
        "RERANK": set(),
    }

    assert _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids) == {}


def test_compute_model_id_renames_does_not_cross_usage():
    old_model_ids = [
        {"model_id": "shared-model", "usage": "CHAT"},
    ]
    new_model_ids = [
        {"model_id": "shared-model-new", "usage": "EMBEDDING"},
    ]
    referenced_model_ids = {
        "CHAT": {"shared-model"},
        "EMBEDDING": set(),
        "RERANK": set(),
    }

    assert _compute_model_id_renames(old_model_ids, new_model_ids, referenced_model_ids) == {}


def test_apply_model_id_renames_updates_referenced_profile_rules():
    configs = {
        "channel": {
            "chat_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "chat-old-a", "priority": 1, "weight": 1},
                    {"channel_id": 1, "model_id": "chat-old-b", "priority": 1, "weight": 1},
                    {"channel_id": 2, "model_id": "other-channel-model", "priority": 1, "weight": 1},
                ],
            },
            "embedding_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "embedding-old", "priority": 1, "weight": 1},
                ],
            },
        },
    }
    renames = {
        "CHAT": {
            "chat-old-a": "chat-new-a",
            "chat-old-b": "chat-new-b",
        },
        "EMBEDDING": {
            "embedding-old": "embedding-new",
        },
    }

    updated_count = _apply_model_id_renames_to_configs(configs, channel_id=1, renames=renames)

    assert updated_count == 3
    assert configs["channel"]["chat_channel"]["rules"][0]["model_id"] == "chat-new-a"
    assert configs["channel"]["chat_channel"]["rules"][1]["model_id"] == "chat-new-b"
    assert configs["channel"]["chat_channel"]["rules"][2]["model_id"] == "other-channel-model"
    assert configs["channel"]["embedding_channel"]["rules"][0]["model_id"] == "embedding-new"
