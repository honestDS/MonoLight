import pytest

import app.core.knowledge.input_limits as input_limits
from app.core.constants import ERR_KB_CONTENT_TOO_LARGE
from app.core.exceptions import ParameterException


@pytest.mark.parametrize(
    ("content", "expected_bytes", "is_too_large"),
    [
        ("知知", 6, False),
        ("知知a", 7, False),
        ("知知ab", 8, True),
    ],
)
def test_validate_knowledge_content_enforces_utf8_byte_limit(monkeypatch, content, expected_bytes, is_too_large):
    monkeypatch.setattr(input_limits, "KNOWLEDGE_CONTENT_MAX_BYTES", 7)
    assert len(content.encode("utf-8")) == expected_bytes

    if is_too_large:
        with pytest.raises(ParameterException) as exc_info:
            input_limits.validate_knowledge_content(content)

        assert exc_info.value.message == ERR_KB_CONTENT_TOO_LARGE
        assert exc_info.value.code == 400
        assert exc_info.value.kwargs == {"max_bytes": 7}
    else:
        assert input_limits.validate_knowledge_content(content) is None
