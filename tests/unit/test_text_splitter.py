import pytest

from app.core.constants import ERR_KB_CHUNK_COUNT_EXCEEDED, ERR_KB_CHUNK_TOTAL_CHARS_EXCEEDED
from app.core.exceptions import ParameterException
from app.core.utils.text_splitter import TextSplitter


@pytest.mark.parametrize(
    "text",
    ["aaaaa\n\nbbbbb", "aaaaabbbbb"],
    ids=["separate-paragraphs", "long-text"],
)
def test_split_respects_max_chunks_and_defaults_to_unlimited(text: str) -> None:
    splitter = TextSplitter(chunk_size=5, chunk_overlap=0)

    assert splitter.split(text, max_chunks=2) == ["aaaaa", "bbbbb"]
    assert splitter.split(text) == ["aaaaa", "bbbbb"]


@pytest.mark.parametrize(
    "text",
    ["aaaaa\n\nbbbbb", "aaaaabbbbb"],
    ids=["separate-paragraphs", "long-text"],
)
def test_split_raises_when_max_chunks_is_exceeded(text: str) -> None:
    splitter = TextSplitter(chunk_size=5, chunk_overlap=0)

    with pytest.raises(ParameterException) as exc_info:
        splitter.split(text, max_chunks=1)

    assert exc_info.value.message == ERR_KB_CHUNK_COUNT_EXCEEDED
    assert exc_info.value.kwargs == {"max_chunks": 1}


def test_split_rejects_high_overlap_chunk_count_instead_of_truncating() -> None:
    splitter = TextSplitter(chunk_size=100, chunk_overlap=99)

    with pytest.raises(ParameterException) as exc_info:
        splitter.split("a" * 700, max_chunks=512)

    assert exc_info.value.message == ERR_KB_CHUNK_COUNT_EXCEEDED
    assert exc_info.value.kwargs == {"max_chunks": 512}


@pytest.mark.parametrize(
    "text, chunk_size, chunk_overlap, max_total_chars, expected_chunks",
    [
        ("abcdefgh", 5, 2, 10, ["abcde", "defgh"]),
        ("abcdefgh", 5, 2, 11, ["abcde", "defgh"]),
        ("abcdefgh", 5, 2, 9, None),
        ("abc\n\ndef", 5, 0, 6, ["abc", "def"]),
        ("abc\n\ndef", 5, 0, 5, None),
    ],
    ids=["overlap-at-limit", "overlap-under-limit", "overlap-over-limit", "paragraphs-at-limit", "paragraphs-over-limit"],
)
def test_split_respects_max_total_chars_by_accumulated_chunk_lengths(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
    max_total_chars: int,
    expected_chunks: list[str] | None,
) -> None:
    splitter = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    if expected_chunks is None:
        with pytest.raises(ParameterException) as exc_info:
            splitter.split(text, max_total_chars=max_total_chars)

        assert exc_info.value.message == ERR_KB_CHUNK_TOTAL_CHARS_EXCEEDED
        assert exc_info.value.kwargs == {"max_chars": max_total_chars}
    else:
        assert splitter.split(text, max_total_chars=max_total_chars) == expected_chunks


def test_reconstruct_chunk_range_restores_paragraph_boundary_without_guessing_overlap() -> None:
    splitter = TextSplitter(chunk_size=5, chunk_overlap=1)

    chunks = splitter.split("beta\n\napple")
    restored = splitter.reconstruct_chunk_range("beta\n\napple", 0, 1)

    assert chunks == ["beta", "apple"]
    assert restored == "beta\n\napple"


def test_reconstruct_chunk_range_removes_only_splitter_created_overlap() -> None:
    splitter = TextSplitter(chunk_size=11, chunk_overlap=5)

    chunks = splitter.split("hello world again")
    restored = splitter.reconstruct_chunk_range("hello world again", 0, 1)

    assert chunks == ["hello world", "world again"]
    assert restored == "hello world again"
