import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.prompts import MARKDOWN_FORMAT_INSTRUCTION_PROMPT, MAX_OUTPUT_TOKENS_INSTRUCTION_PROMPT, SYSTEM_CONTEXT_WRAPPER
from app.core.utils.system import get_full_system_context
from app.models.message import InternalMessage, MessageRole, TextPart


def build_markdown_instruction(enable_markdown: bool) -> str:
    status = "enabled" if enable_markdown else "disabled"
    requirement = "You may use Markdown when it improves clarity." if enable_markdown else "Return plain text only. Do not use Markdown syntax."
    return "\n\n" + MARKDOWN_FORMAT_INSTRUCTION_PROMPT.format(status=status, requirement=requirement)


def append_text_instruction(message: InternalMessage, instruction: str) -> InternalMessage:
    if isinstance(message.content, str):
        message.content = f"{message.content}{instruction}" if message.content else instruction.strip()
        return message

    if isinstance(message.content, list):
        for part in reversed(message.content):
            if isinstance(part, TextPart):
                part.text = f"{part.text}{instruction}"
                return message
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = f"{part.get('text', '')}{instruction}"
                return message
        message.content.append(TextPart(text=instruction.strip()))
        return message

    message.content = instruction.strip()
    return message


def build_max_output_tokens_instruction(max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    return "\n\n" + MAX_OUTPUT_TOKENS_INSTRUCTION_PROMPT.format(max_tokens=max_tokens)


def materialize_user_environment_prompts(messages: list[InternalMessage]) -> list[InternalMessage]:
    request_messages = [message.model_copy(deep=True) for message in messages]
    latest_user_message: InternalMessage | None = None
    for message in request_messages:
        if message.role != MessageRole.USER:
            continue
        latest_user_message = message
        environment_prompt = message.environment_prompt if isinstance(message.environment_prompt, str) else ""
        if environment_prompt:
            append_text_instruction(message, environment_prompt)

    if latest_user_message is not None:
        guidance_prompt = latest_user_message.guidance_prompt.strip() if isinstance(latest_user_message.guidance_prompt, str) else ""
        if guidance_prompt:
            append_text_instruction(latest_user_message, f"\n\n{guidance_prompt}")
    return request_messages


def build_runtime_environment_instruction() -> str:
    return "\n\n" + SYSTEM_CONTEXT_WRAPPER.format(context=get_full_system_context())


async def build_user_runtime_instructions(db: AsyncSession, session_id: str, max_tokens: int = 0) -> str:
    session = await session_crud.get_by_session_id(db, session_id)
    enable_markdown = session.enable_markdown if session else False
    return build_markdown_instruction(enable_markdown) + build_max_output_tokens_instruction(max_tokens) + build_runtime_environment_instruction()


def append_user_runtime_instruction_text(message: InternalMessage, instruction: str) -> InternalMessage:
    message.environment_prompt = instruction
    return message


async def ensure_user_runtime_instructions(
    db: AsyncSession,
    session_id: str,
    message: InternalMessage,
    max_tokens: int = 0,
    *,
    instruction: str | None = None,
) -> InternalMessage:
    if isinstance(message.environment_prompt, str) and message.environment_prompt:
        return message

    if instruction is None:
        instruction = await build_user_runtime_instructions(db, session_id, max_tokens)
    append_user_runtime_instruction_text(message, instruction)
    if message.id is not None:
        await message_crud.set_environment_prompt(db, message.id, instruction)
    return message


def refresh_max_output_tokens_instruction(message: InternalMessage, max_tokens: int) -> InternalMessage:
    environment_prompt = message.environment_prompt if isinstance(message.environment_prompt, str) else ""
    pattern = r"(The hard maximum for this response is )\d+( output tokens\.)"
    replacement = rf"\g<1>{max_tokens}\2"
    if re.search(pattern, environment_prompt):
        message.environment_prompt = re.sub(pattern, replacement, environment_prompt, count=1)
    else:
        message.environment_prompt = f"{environment_prompt}{build_max_output_tokens_instruction(max_tokens)}"
    return message


async def refresh_latest_user_max_output_tokens_instruction(
    db: AsyncSession,
    messages: list[InternalMessage],
    max_tokens: int,
) -> None:
    for message in reversed(messages):
        if message.role != MessageRole.USER:
            continue
        refresh_max_output_tokens_instruction(message, max_tokens)
        if message.id is not None:
            await message_crud.set_environment_prompt(db, message.id, message.environment_prompt)
        return
