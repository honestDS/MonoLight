import re

import pytest

from app.core import constants
from app.core.i18n import t
from app.core.utils.context_summary import split as split_module
from app.core.utils.context_summary.split import split_oversized_message
from app.models.message import InternalMessage, MessageRole


def test_message_within_budget_remains_single_source_unit(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", len)
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
    assert "<message_chunk" not in units[0].content


def test_oversized_tool_result_is_split_into_bounded_ordered_parts(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", len)
    message = InternalMessage(
        id=7,
        role=MessageRole.TOOL,
        content="tool-result-" * 80,
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
    assert [int(re.search(r'part="(\d+)"', unit.content).group(1)) for unit in units] == list(range(len(units)))
    assert all('message_id="7"' in unit.content for unit in units)
    assert all('role="tool"' in unit.content for unit in units)


def test_message_chunk_metadata_must_fit_budget(monkeypatch):
    monkeypatch.setattr(split_module, "estimate_tokens", len)
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

    assert str(exc_info.value) == t(constants.ERR_CONTEXT_SUMMARY_CHUNK_METADATA_OVER_BUDGET)
