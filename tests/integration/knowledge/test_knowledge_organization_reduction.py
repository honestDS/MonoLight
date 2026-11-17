from __future__ import annotations

import hashlib

import pytest

from app.core.knowledge import organization_reduction
from app.core.knowledge.organization_executor import KnowledgeOrganizationScopeItem, execute_knowledge_organization
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from tests.integration.knowledge import test_knowledge_organization_execution as execution_module
from tests.integration.knowledge.test_knowledge_organization_execution import (
    _add_item,
    _create_managed_container,
    _keep_plan_for_scope,
    _model,
)

session_factory = execution_module.session_factory


@pytest.mark.asyncio
async def test_reduction_keep_preserves_lower_update_action(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="alpha", content=("alpha detail " * 120).strip())
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="beta", content=("beta detail " * 120).strip())

    async def model_caller(_model_config, *, scope):
        if all(item.source_type != "organization_fragment" for item in scope):
            items = []
            for input_item in scope:
                knowledge_id, expected_version = input_item.sources[0]
                if knowledge_id == 1:
                    items.append(
                        {
                            "action": "update",
                            "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                            "target": {"knowledge_key": "alpha-updated", "content": "updated alpha content"},
                            "summary": "updated alpha",
                        }
                    )
                else:
                    items.append(
                        {
                            "action": "keep",
                            "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                            "summary": "kept beta",
                        }
                    )
            return KnowledgeOrganizationPlan.model_validate({"items": items})

        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                        "summary": "x",
                    }
                    for input_item in scope
                    for knowledge_id, expected_version in input_item.sources
                ]
            }
        )

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=360),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    actions = {item.source.knowledge_id: item for item in result.plan.items}
    assert actions[1].action == "update"
    assert actions[1].target.knowledge_key == "alpha-updated"
    assert actions[1].target.content == "updated alpha content"
    assert actions[2].action == "keep"


@pytest.mark.asyncio
async def test_reduction_can_merge_multiple_lower_actions_without_new_action_types(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="alpha", content=("alpha detail " * 120).strip())
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="beta", content=("beta detail " * 120).strip())

    async def model_caller(_model_config, *, scope):
        if all(item.source_type != "organization_fragment" for item in scope):
            return _keep_plan_for_scope(scope)
        sources = [{"knowledge_id": knowledge_id, "expected_version": expected_version} for input_item in scope for knowledge_id, expected_version in input_item.sources]
        if len(sources) == 1:
            return KnowledgeOrganizationPlan.model_validate({"items": [{"action": "keep", "source": sources[0], "summary": "x"}]})
        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "merge",
                        "sources": sources,
                        "primary_knowledge_id": sources[0]["knowledge_id"],
                        "target": {"knowledge_key": "alpha-beta", "content": "merged alpha beta content"},
                        "summary": "merged",
                    }
                ]
            }
        )

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=360),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert len(result.plan.items) == 1
    assert result.plan.items[0].action == "merge"
    assert {source.knowledge_id for source in result.plan.items[0].sources} == {1, 2}
    assert {item.action for item in result.plan.items} <= {"keep", "update", "merge", "conflict"}


def test_reduction_preserves_existing_merge_without_new_action_type():
    lower_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "alpha-beta", "content": "canonical merged alpha beta content"},
                    "summary": "lower merged summary",
                }
            ]
        }
    )
    lower_item = lower_plan.items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="alpha-beta",
            content="lower merged summary",
            content_hash=hashlib.sha256(b"canonical merged alpha beta content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_item,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "alpha-beta", "content": "lower merged summary"},
                    "summary": "still merged",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert len(effective_plan.items) == 1
    assert effective_plan.items[0].action == "merge"
    assert effective_plan.items[0].target.content == "canonical merged alpha beta content"
    assert output_scope[0].effective_item == effective_plan.items[0]
    assert {item.action for item in effective_plan.items} <= {"keep", "update", "merge", "conflict"}


def test_reduction_new_single_source_update_replaces_lower_update():
    lower_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "topic-v1", "content": "first proposed content"},
                    "summary": "first proposal",
                }
            ]
        }
    )
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-v1",
            content="first proposal",
            content_hash=hashlib.sha256(b"first proposed content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_plan.items[0],
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "topic-v2", "content": "second proposed content"},
                    "summary": "second proposal",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert effective_plan.items[0].action == "update"
    assert effective_plan.items[0].target.knowledge_key == "topic-v2"
    assert effective_plan.items[0].target.content == "second proposed content"
    assert output_scope[0].effective_item == effective_plan.items[0]


def test_schema_rejects_single_source_conflict_before_reduction_composition():
    with pytest.raises(ValueError):
        KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "conflict",
                        "sources": [{"knowledge_id": 1, "expected_version": 1}],
                        "reason": "uncertain",
                        "summary": "uncertain",
                    }
                ]
            }
        )


@pytest.mark.parametrize("upper_action", ["conflict", "merge"])
def test_reduction_cannot_change_existing_multi_source_action_without_combining_candidates(upper_action: str):
    lower_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "merged", "content": "canonical merged content"},
                    "summary": "merged summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="merged",
            content="merged summary",
            content_hash=hashlib.sha256(b"canonical merged content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_merge,
        ),
    )
    if upper_action == "conflict":
        raw_item = {
            "action": "conflict",
            "sources": [
                {"knowledge_id": 1, "expected_version": 1},
                {"knowledge_id": 2, "expected_version": 1},
            ],
            "reason": "changed interpretation",
            "summary": "changed",
        }
    else:
        raw_item = {
            "action": "merge",
            "sources": [
                {"knowledge_id": 1, "expected_version": 1},
                {"knowledge_id": 2, "expected_version": 1},
            ],
            "primary_knowledge_id": 1,
            "target": {"knowledge_key": "different", "content": "different merged target"},
            "summary": "changed",
        }
    upper_plan = KnowledgeOrganizationPlan.model_validate({"items": [raw_item]})

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(upper_plan, scope=scope)


def test_reduction_existing_conflict_keeps_effective_action_and_accepts_new_compact_summary():
    lower_conflict = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "lower unresolved reason",
                    "summary": "lower conflict summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="conflict-1-2",
            content="lower conflict summary",
            content_hash=hashlib.sha256(b"lower conflict summary").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_conflict,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "upper comparison still unresolved",
                    "summary": "new compact conflict summary",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert effective_plan.items[0] == lower_conflict
    assert output_scope[0].effective_item == lower_conflict
    assert output_scope[0].content == "new compact conflict summary"


def test_reduction_existing_merge_rejects_primary_change_even_when_compact_target_matches():
    lower_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "merged", "content": "canonical merged content"},
                    "summary": "compact merged summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="merged",
            content="compact merged summary",
            content_hash=hashlib.sha256(b"canonical merged content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_merge,
        ),
    )
    changed_primary = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 2,
                    "target": {"knowledge_key": "merged", "content": "compact merged summary"},
                    "summary": "still merged",
                }
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(changed_primary, scope=scope)


def test_reduction_can_create_conflict_by_combining_multiple_previous_candidates():
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-a",
            content="alpha",
            content_hash=hashlib.sha256(b"alpha").hexdigest(),
            source_type="organization_fragment",
        ),
        KnowledgeOrganizationScopeItem(
            sources=((2, 1),),
            knowledge_key="topic-b",
            content="beta",
            content_hash=hashlib.sha256(b"beta").hexdigest(),
            source_type="organization_fragment",
        ),
    )
    upper_conflict = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "incompatible facts",
                    "summary": "conflicting alpha and beta",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_conflict, scope=scope)

    assert effective_plan.items[0].action == "conflict"
    assert output_scope[0].sources == ((1, 1), (2, 1))


def test_reduction_rejects_single_candidate_merge_without_combining_previous_candidates():
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-a",
            content="alpha",
            content_hash=hashlib.sha256(b"alpha").hexdigest(),
            source_type="organization_fragment",
        ),
    )
    invalid_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 1, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "topic-a", "content": "alpha"},
                    "summary": "invalid merge",
                }
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(invalid_merge, scope=scope)


def test_reduction_keep_revalidates_inherited_targets_across_fragments():
    first_update = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "shared-target", "content": "first revised content"},
                    "summary": "first update",
                }
            ]
        }
    ).items[0]
    second_update = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "target": {"knowledge_key": "shared-target", "content": "second revised content"},
                    "summary": "second update",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="shared-target",
            content="first update",
            content_hash=hashlib.sha256(b"first revised content").hexdigest(),
            source_type="organization_fragment",
            effective_item=first_update,
        ),
        KnowledgeOrganizationScopeItem(
            sources=((2, 1),),
            knowledge_key="shared-target",
            content="second update",
            content_hash=hashlib.sha256(b"second revised content").hexdigest(),
            source_type="organization_fragment",
            effective_item=second_update,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "summary": "keep first",
                },
                {
                    "action": "keep",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "summary": "keep second",
                },
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(upper_plan, scope=scope)
