from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.core.prompts import MARKDOWN_FORMAT_INSTRUCTION_PROMPT, MAX_OUTPUT_TOKENS_INSTRUCTION_PROMPT, SYSTEM_CONTEXT_WRAPPER
from app.core.utils.system import get_full_system_context
from app.models.message import InternalMessage, MessageRole, TextPart


def build_markdown_instruction(enable_markdown: bool) -> str:
    status = "开启" if enable_markdown else "关闭"
    requirement = "可以使用 Markdown 格式组织回答。" if enable_markdown else "请使用纯文本回答，避免 Markdown 标记。"
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


def append_environment_prompt_instruction(message: InternalMessage, instruction: str) -> InternalMessage:
    message.environment_prompt = f"{message.environment_prompt or ''}{instruction}"
    return message


async def materialize_latest_user_environment_prompt(
    db: AsyncSession,
    session_id: str,
    messages: list[InternalMessage],
    max_tokens: int,
) -> list[InternalMessage]:
    request_messages = [message.model_copy(deep=True) for message in messages]
    for message in reversed(request_messages):
        if message.role != MessageRole.USER or (message.id is None and not message.environment_prompt):
            continue
        message.environment_prompt = await build_user_runtime_instructions(db, session_id, max_tokens)
        if message.id is not None:
            await message_crud.set_environment_prompt(db, message.id, message.environment_prompt)
        append_text_instruction(message, message.environment_prompt)
        break
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


async def append_user_runtime_instructions(db: AsyncSession, session_id: str, message: InternalMessage, max_tokens: int = 0) -> InternalMessage:
    return append_user_runtime_instruction_text(message, await build_user_runtime_instructions(db, session_id, max_tokens))
