"""渠道路由器：负责按优先级分组 + 组内加权轮询选择渠道

调度策略：
- 按 priority 升序逐组尝试（越小越优先）。
- 同一优先级组内按 weight 进行加权轮询：weight 表示一个轮询周期内该渠道被使用的次数。
  例如 A 权重 1、B 权重 2，则按 A → B → B → A → B → B ... 循环使用。
- 渠道调用失败时由调用方将该 priority 加入 excluded_priorities 再次调用，直接降级到下一优先级组，
  不在同一优先级组内重试。
"""

from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import t
from app.core.log import get_logger
from app.models.channel import ChannelConfig, ChannelRule
from app.models.provider import ModelProvider

logger = get_logger(__name__)


def _get_model_entry(provider: ModelProvider, model_id: str, expected_usage: str) -> dict | None:
    """从 Provider 的 model_ids 中查找匹配 model_id 且 usage 一致的条目。

    允许同一 model_id 配置多个不同 usage 的条目，因此匹配时需同时校验 usage，
    避免命中同名但用途不符的条目。
    """
    for entry in provider.model_ids:
        if entry.get("model_id") == model_id and entry.get("usage") == expected_usage:
            return entry
    return None


def _expand_by_weight(rules: list[ChannelRule]) -> list[ChannelRule]:
    """按 weight 展开为轮询序列；weight=0 的渠道不参与轮询。

    展开前先按稳定唯一键 (provider_id, model_id) 排序，使轮询序列与
    渠道在 rules 列表中的顺序无关：仅调整列表顺序（不改 priority/weight）
    不会影响同优先级组内的加权轮询计算。
    """
    sorted_rules = sorted(rules, key=lambda r: (r.provider_id, r.model_id))
    expanded: list[ChannelRule] = []
    for rule in sorted_rules:
        w = max(int(rule.weight), 0)
        expanded.extend([rule] * w)
    return expanded


async def _pick_round_robin(rules: list[ChannelRule], cursor_key: str, priority: int) -> ChannelRule | None:
    """按 weight 加权轮询选取一条规则，并推进持久化游标。

    若 cursor_key 为空则视为"探测"调用，取展开序列首项且不推进游标。
    游标位置持久化在数据库（channel_cursor 表，由游标 CRUD 用独立会话管理），支持多 worker 共享。
    """
    expanded = _expand_by_weight(rules)
    if not expanded:
        return None

    if not cursor_key:
        return expanded[0]

    from app.core.crud.channel_cursor import channel_cursor_crud

    idx = await channel_cursor_crud.next_index(f"{cursor_key}:{priority}", len(expanded))
    return expanded[idx]


async def select_channel(
    db: AsyncSession,
    channel_cfg: ChannelConfig,
    expected_usage: str,
    call_context: str | None = None,
    excluded_priorities: set[int] | None = None,
    cursor_key: str | None = None,
    log_selection: bool = True,
) -> tuple[ModelProvider, dict, ChannelRule] | None:
    """从渠道配置中按优先级分组 + 组内加权轮询选择一个可用渠道。

    调度策略：按 priority 升序逐组尝试；组内按 weight 加权轮询选出唯一渠道，
    不在同一优先级组内重试。调用方在选中渠道失败后应将该 priority 加入
    excluded_priorities 再次调用，从而直接降级到下一优先级组。

    Args:
        db: 数据库会话
        channel_cfg: 渠道配置
        expected_usage: 期望的用途（"CHAT"/"EMBEDDING"/"RERANK"）
        call_context: 调用场景，用于区分主对话、标题生成、知识库检索等日志来源
        excluded_priorities: 本次调用中已失败或需跳过的优先级组集合
        cursor_key: 加权轮询游标键（建议为 "{profile_id}:{usage}"）；
            传入时按轮询推进游标，不传时取组内首项且不推进（用于可用性探测）
        log_selection: 是否在选中渠道后记录"选择渠道"日志；
            可用性探测等非真实调用场景应传 False 以避免日志噪声

    Returns:
        (provider, model_entry, rule) 三元组，若没有可用渠道则返回 None
    """
    if not channel_cfg or not channel_cfg.rules:
        logger.warning(t("LOG_CHANNEL_CONFIG_EMPTY", expected_usage=expected_usage))
        return None

    from app.core.crud.provider import provider_crud

    excluded_priorities = excluded_priorities or set()

    # 收集所有规则，按 priority 分组；启用状态由模型管理页的模型条目控制
    priority_groups: dict[int, list[ChannelRule]] = defaultdict(list)
    for rule in channel_cfg.rules:
        priority_groups[rule.priority].append(rule)

    if not priority_groups:
        logger.warning(t("LOG_CHANNEL_NO_RULES"))
        return None

    # 按 priority 升序尝试（越小越优先）
    sorted_priorities = sorted(priority_groups.keys())

    for priority in sorted_priorities:
        # 跳过本次调用中已失败的优先级组，直接降级到下一组
        if priority in excluded_priorities:
            continue

        group_rules = priority_groups[priority]

        # 组内按 weight 加权轮询选择一个渠道，不在组内重试
        selected_rule = await _pick_round_robin(group_rules, cursor_key or "", priority)
        if not selected_rule:
            logger.bind(priority=priority).warning(t("LOG_CHANNEL_ZERO_WEIGHT"))
            continue

        # 校验 Provider 存在且有效
        provider = await provider_crud.get(db, selected_rule.provider_id)
        if not provider or not provider.is_active:
            logger.bind(provider_id=selected_rule.provider_id).warning(t("LOG_CHANNEL_PROVIDER_UNAVAILABLE"))
            continue

        # 校验 Provider 的 model_ids 中存在 model_id 且 usage 匹配的条目
        model_entry = _get_model_entry(provider, selected_rule.model_id, expected_usage)
        if not model_entry:
            logger.bind(
                provider_id=provider.id,
                model_id=selected_rule.model_id,
                expected_usage=expected_usage,
            ).warning(t("LOG_CHANNEL_MODEL_ENTRY_NOT_FOUND"))
            continue

        if log_selection:
            channel_name = f"{provider.name} / {selected_rule.model_id}"
            logger.bind(
                provider_id=provider.id,
                provider_name=provider.name,
                model_id=selected_rule.model_id,
                model_name=selected_rule.model_id,
                channel_name=channel_name,
                priority=priority,
                expected_usage=expected_usage,
                call_context=call_context or "unspecified",
            ).info(t("LOG_CHANNEL_SELECTED", channel_name=channel_name))

        return provider, model_entry, selected_rule

    logger.warning(t("LOG_CHANNEL_ALL_UNAVAILABLE", expected_usage=expected_usage))
    return None
