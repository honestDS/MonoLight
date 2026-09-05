from app.core.utils.text_splitter import TextSplitter


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
