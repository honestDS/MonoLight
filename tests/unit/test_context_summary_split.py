import json

import pytest

from app.core.constants import ERR_CONTEXT_SUMMARY_CHUNK_METADATA_OVER_BUDGET
from app.core.i18n import t
from app.core.utils.context_summary import split as split_module
from app.core.utils.context_summary.common import serialize_message
from app.core.utils.context_summary.split import split_oversized_message
from app.models.message import InternalMessage, MessageRole


def test_message_within_budget_remains_single_source_unit(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", lambda text, **_kwargs: len(text))
    message = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        content="short result",
        tool_call_id="call-1",
    )

    units = list(
        split_oversized_message(
            message,
            max_unit_tokens=1000,
        )
    )

    assert len(units) == 1
    assert units[0].message_start_id == 7
    assert units[0].message_end_id == 7
    assert units[0].token_count == len(units[0].content)
    assert json.loads(units[0].content)["message_chunks"] == [json.loads(serialize_message(message))]


def test_oversized_tool_result_is_split_into_bounded_ordered_parts(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", lambda text, **_kwargs: len(text))
    message = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        content='中文结果\n包含"引号"和<xml>data</xml>\n' * 80,
        tool_call_id="call-1",
    )

    units = list(
        split_oversized_message(
            message,
            max_unit_tokens=160,
        )
    )

    assert len(units) > 1
    assert all(unit.message_start_id == 7 for unit in units)
    assert all(unit.message_end_id == 7 for unit in units)
    assert all(unit.token_count == len(unit.content) <= 160 for unit in units)
    chunks = [json.loads(unit.content)["message_chunks"] for unit in units]
    assert all(len(chunk) == 1 for chunk in chunks)
    parts = [chunk[0] for chunk in chunks]
    assert [part["part"] for part in parts] == list(range(len(parts)))
    assert all(part["message_id"] == 7 for part in parts)
    assert all(part["role"] == "tool" for part in parts)
    reconstructed = "".join(part["content"] for part in parts)
    assert reconstructed == serialize_message(message)
    assert json.loads(reconstructed)["content"] == message.content


def test_message_chunk_metadata_must_fit_budget(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", lambda text, **_kwargs: len(text))
    message = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        content="large result",
        tool_call_id="call-1",
    )

    with pytest.raises(RuntimeError) as exc_info:
        list(
            split_oversized_message(
                message,
                max_unit_tokens=1,
            )
        )

    assert str(exc_info.value) == t(ERR_CONTEXT_SUMMARY_CHUNK_METADATA_OVER_BUDGET)
