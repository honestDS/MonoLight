"""channel_router单元测试：优先级分组、加权轮询、游标推进机制"""

from unittest.mock import AsyncMock

import pytest

from app.core.channel_router import (
    _expand_by_weight,
    _get_model_entry,
    _pick_round_robin,
    select_channel,
)
from app.core.crud.channel import channel_crud
from app.core.crud.channel_cursor import channel_cursor_crud
from app.models.channel import ChannelConfig, ChannelRule, ModelChannel


def test_expand_by_weight_stable_order():
    """测试加权展开的稳定性：仅调整列表顺序不应影响轮询结果"""
    rules_order_a = [
        ChannelRule(channel_id=1, model_id="model-a", priority=1, weight=2),
        ChannelRule(channel_id=2, model_id="model-b", priority=1, weight=1),
    ]
    rules_order_b = [
        ChannelRule(channel_id=2, model_id="model-b", priority=1, weight=1),
        ChannelRule(channel_id=1, model_id="model-a", priority=1, weight=2),
    ]

    expanded_a = _expand_by_weight(rules_order_a)
    expanded_b = _expand_by_weight(rules_order_b)

    # 展开结果应相同（已按channel_id, model_id排序）
    assert len(expanded_a) == 3
    assert len(expanded_b) == 3
    assert expanded_a[0].channel_id == expanded_b[0].channel_id
    assert expanded_a[1].channel_id == expanded_b[1].channel_id
    assert expanded_a[2].channel_id == expanded_b[2].channel_id


def test_expand_by_weight_zero_weight_excluded():
    """测试权重为0的规则不参与轮询"""
    rules = [
        ChannelRule(channel_id=1, model_id="model-a", priority=1, weight=0),
        ChannelRule(channel_id=2, model_id="model-b", priority=1, weight=1),
    ]

    expanded = _expand_by_weight(rules)

    assert len(expanded) == 1
    assert expanded[0].channel_id == 2


def test_get_model_entry_usage_match():
    """测试model_entry查找时必须同时匹配model_id和usage"""
    channel = ModelChannel(
        name="test-channel",
        channel_type="OPENAI",
        api_key="fake-key",
        model_ids=[
            {"model_id": "gpt-4o", "usage": "CHAT"},
            {"model_id": "gpt-4o", "usage": "EMBEDDING"},
        ],
    )

    # 查找CHAT用途
    entry_chat = _get_model_entry(channel, "gpt-4o", "CHAT")
    assert entry_chat is not None
    assert entry_chat["usage"] == "CHAT"

    # 查找EMBEDDING用途
    entry_embedding = _get_model_entry(channel, "gpt-4o", "EMBEDDING")
    assert entry_embedding is not None
    assert entry_embedding["usage"] == "EMBEDDING"

    # 查找不存在的RERANK用途
    entry_rerank = _get_model_entry(channel, "gpt-4o", "RERANK")
    assert entry_rerank is None


@pytest.mark.asyncio
async def test_select_channel_priority_fallback(monkeypatch):
    """测试优先级降级：第一组失败后降级到第二组"""
    mock_db = AsyncMock()

    # 创建两个渠道
    channel_1 = ModelChannel(
        id=1,
        name="channel-1",
        channel_type="OPENAI",
        api_key="key-1",
        base_url="https://api1.com",
        is_active=False,  # 第一个渠道不可用
        model_ids=[{"model_id": "gpt-4o", "usage": "CHAT", "is_enabled": True}],
    )
    channel_2 = ModelChannel(
        id=2,
        name="channel-2",
        channel_type="OPENAI",
        api_key="key-2",
        base_url="https://api2.com",
        is_active=True,
        model_ids=[{"model_id": "gpt-4o-mini", "usage": "CHAT", "is_enabled": True}],
    )

    async def mock_get(db, channel_id):
        if channel_id == 1:
            return channel_1
        if channel_id == 2:
            return channel_2
        return None

    monkeypatch.setattr(channel_crud, "get", mock_get)

    # 配置：两个优先级组
    channel_config = ChannelConfig(
        rules=[
            ChannelRule(channel_id=1, model_id="gpt-4o", priority=1, weight=100),
            ChannelRule(channel_id=2, model_id="gpt-4o-mini", priority=2, weight=100),
        ]
    )

    async def mock_pick_round_robin(rules, cursor_key, priority):
        return rules[0] if rules else None

    monkeypatch.setattr("app.core.channel_router._pick_round_robin", mock_pick_round_robin)

    # 执行选择
    result = await select_channel(
        db=mock_db,
        channel_cfg=channel_config,
        expected_usage="CHAT",
        log_selection=False,
    )

    # 应该跳过priority=1（channel不活跃），选择priority=2
    assert result is not None
    channel, model_entry, rule = result
    assert channel.id == 2
    assert rule.priority == 2


@pytest.mark.asyncio
async def test_select_channel_model_entry_disabled(monkeypatch):
    """测试模型条目被禁用时跳过"""
    mock_db = AsyncMock()

    channel = ModelChannel(
        id=1,
        name="test-channel",
        channel_type="OPENAI",
        api_key="fake-key",
        base_url="https://api.com",
        is_active=True,
        model_ids=[
            {"model_id": "gpt-4o", "usage": "CHAT", "is_enabled": False},  # 被禁用
        ],
    )

    monkeypatch.setattr(channel_crud, "get", AsyncMock(return_value=channel))

    channel_config = ChannelConfig(rules=[ChannelRule(channel_id=1, model_id="gpt-4o", priority=1, weight=100)])

    async def mock_pick_round_robin(rules, cursor_key, priority):
        return ChannelRule(channel_id=1, model_id="gpt-4o", priority=1, weight=100)

    monkeypatch.setattr("app.core.channel_router._pick_round_robin", mock_pick_round_robin)

    result = await select_channel(
        db=mock_db,
        channel_cfg=channel_config,
        expected_usage="CHAT",
        log_selection=False,
    )

    # 模型被禁用，应返回None
    assert result is None


def test_expand_by_weight_correct_repetition():
    """测试加权展开按权重重复正确次数"""
    rules = [
        ChannelRule(channel_id=1, model_id="model-a", priority=1, weight=3),
        ChannelRule(channel_id=2, model_id="model-b", priority=1, weight=2),
    ]

    expanded = _expand_by_weight(rules)

    # 应该有5个元素：3个model-a + 2个model-b
    assert len(expanded) == 5
    channel_ids = [r.channel_id for r in expanded]
    assert channel_ids.count(1) == 3
    assert channel_ids.count(2) == 2


@pytest.mark.asyncio
async def test_pick_round_robin_advances_weight_cursor(monkeypatch):
    """测试真实选择渠道会推进组内权重游标"""
    rules = [
        ChannelRule(channel_id=1, model_id="model-a", priority=1, weight=1),
        ChannelRule(channel_id=2, model_id="model-b", priority=1, weight=1),
    ]

    next_index = AsyncMock(return_value=1)
    monkeypatch.setattr(channel_cursor_crud, "next_index", next_index)

    selected_rule = await _pick_round_robin(rules, "profile-1:CHAT", 1)

    assert selected_rule is not None
    assert selected_rule.channel_id == 2
    next_index.assert_awaited_once_with("profile-1:CHAT:1", 2)
