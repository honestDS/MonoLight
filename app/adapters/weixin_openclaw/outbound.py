from app.adapters.weixin_openclaw.constants import (
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_ASCII_CHAR_LIMIT,
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_CHINESE_CHAR_LIMIT,
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_MAX_REFINEMENT_ATTEMPTS,
    WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
)
from app.core.constants import MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED
from app.core.message_platforms.outbound_text import OutboundTextPolicy
from app.core.prompts import WEIXIN_OPENCLAW_CONCISE_OUTPUT_SYSTEM_PROMPT, WEIXIN_OPENCLAW_CONCISE_RETRY_PROMPT


def build_weixin_openclaw_concise_output_system_prompt() -> str:
    return WEIXIN_OPENCLAW_CONCISE_OUTPUT_SYSTEM_PROMPT.format(
        chinese_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_CHINESE_CHAR_LIMIT,
        ascii_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_ASCII_CHAR_LIMIT,
        utf8_byte_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
    )


def build_weixin_openclaw_concise_retry_prompt() -> str:
    return WEIXIN_OPENCLAW_CONCISE_RETRY_PROMPT.format(
        chinese_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_CHINESE_CHAR_LIMIT,
        ascii_char_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_ASCII_CHAR_LIMIT,
        utf8_byte_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
    )


WEIXIN_OPENCLAW_OUTBOUND_TEXT_POLICY = OutboundTextPolicy(
    utf8_byte_limit=WEIXIN_OPENCLAW_OUTBOUND_TEXT_UTF8_BYTE_LIMIT,
    max_refinement_attempts=WEIXIN_OPENCLAW_OUTBOUND_TEXT_MAX_REFINEMENT_ATTEMPTS,
    additional_system_prompt=build_weixin_openclaw_concise_output_system_prompt(),
    refinement_prompt=build_weixin_openclaw_concise_retry_prompt(),
    refinement_failed_message_key=MSG_WEIXIN_OPENCLAW_OUTBOUND_TEXT_REFINEMENT_FAILED,
    max_text_parts=2,
)
