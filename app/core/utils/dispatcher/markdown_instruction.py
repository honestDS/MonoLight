from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session import session_crud
from app.core.prompts import MARKDOWN_FORMAT_INSTRUCTION_PROMPT, SYSTEM_CONTEXT_WRAPPER
from app.core.utils.system import get_full_system_context
from app.models.message import InternalMessage, TextPart


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


def build_runtime_environment_instruction() -> str:
    return "\n\n" + SYSTEM_CONTEXT_WRAPPER.format(context=get_full_system_context())


async def build_user_runtime_instructions(db: AsyncSession, session_id: str) -> str:
    session = await session_crud.get_by_session_id(db, session_id)
    enable_markdown = session.enable_markdown if session else False
    return build_markdown_instruction(enable_markdown) + build_runtime_environment_instruction()


def append_user_runtime_instruction_text(message: InternalMessage, instruction: str) -> InternalMessage:
    return append_text_instruction(message, instruction)


async def append_user_runtime_instructions(db: AsyncSession, session_id: str, message: InternalMessage) -> InternalMessage:
    return append_user_runtime_instruction_text(message, await build_user_runtime_instructions(db, session_id))
