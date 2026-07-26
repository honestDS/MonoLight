from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K
from app.core.utils.dispatcher.helpers import resolve_chat_params
from app.core.utils.http_proxy import get_channel_http_proxy
from app.models.channel import ChannelConfig, ModelUsage, resolve_model_protocol


@dataclass(frozen=True)
class ContextSummaryModelSnapshot:
    channel_id: int
    channel_name: str
    model_id: str
    protocol: str
    base_url: str | None
    api_key: str
    priority: int
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    http_proxy: str | None = None

    def accepts_prompt_tokens(self, prompt_tokens: int) -> bool:
        return prompt_tokens <= self.input_budget_tokens


async def select_context_summary_model(
    db: AsyncSession,
    *,
    profile_id: int,
    channel_config: ChannelConfig,
    safety_margin_tokens: int,
    excluded_priorities: set[int] | None = None,
    call_context: str = "context_summary",
) -> ContextSummaryModelSnapshot | None:
    selection = await select_channel(
        db,
        channel_config,
        ModelUsage.CHAT.value,
        call_context=call_context,
        excluded_priorities=excluded_priorities,
        cursor_key=f"{profile_id}:{ModelUsage.CHAT.value}:CONTEXT_SUMMARY",
    )
    if selection is None:
        return None

    channel, model_entry, rule = selection
    chat_params = resolve_chat_params(model_entry, channel_config)
    context_window_tokens = chat_params["context_window_k"] * CONTEXT_WINDOW_TOKENS_PER_K
    max_output_tokens = min(1024, max(256, context_window_tokens // 16))
    normalized_safety_margin = max(safety_margin_tokens, 0)

    return ContextSummaryModelSnapshot(
        channel_id=channel.id,
        channel_name=channel.name,
        model_id=model_entry["model_id"],
        protocol=resolve_model_protocol(model_entry),
        base_url=channel.base_url,
        api_key=channel.get_decrypted_api_key(),
        priority=rule.priority,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        safety_margin_tokens=normalized_safety_margin,
        input_budget_tokens=max(
            1,
            context_window_tokens - max_output_tokens - normalized_safety_margin,
        ),
        http_proxy=get_channel_http_proxy(channel),
    )
