import io

import pytest
from fastapi import UploadFile

from app.api.knowledge_input import read_knowledge_file
from app.core.constants import (
    ERR_KB_FILE_TOO_LARGE,
    KNOWLEDGE_FILE_MAX_BYTES,
    KNOWLEDGE_FILE_READ_CHUNK_BYTES,
)
from app.core.exceptions import ParameterException


class RecordingBytesIO(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


@pytest.mark.parametrize(
    ("content_size", "declared_size"),
    [
        (0, KNOWLEDGE_FILE_MAX_BYTES + 1),
        (KNOWLEDGE_FILE_MAX_BYTES - 1, 0),
        (KNOWLEDGE_FILE_MAX_BYTES, 123),
    ],
)
async def test_read_knowledge_file_accepts_content_up_to_limit(content_size, declared_size):
    content = bytes(index % 256 for index in range(content_size))
    source = RecordingBytesIO(content)
    upload = UploadFile(file=source, filename="knowledge.bin", size=declared_size)

    result = await read_knowledge_file(upload)

    assert result == content
    assert source.read_sizes
    assert all(size == KNOWLEDGE_FILE_READ_CHUNK_BYTES for size in source.read_sizes)


async def test_read_knowledge_file_rejects_content_over_limit():
    content = bytes(index % 256 for index in range(KNOWLEDGE_FILE_MAX_BYTES + 1))
    source = RecordingBytesIO(content)
    upload = UploadFile(file=source, filename="knowledge.bin", size=0)

    with pytest.raises(ParameterException) as exc_info:
        await read_knowledge_file(upload)

    assert exc_info.value.message_spec.key == ERR_KB_FILE_TOO_LARGE
    assert exc_info.value.code == 413
    assert exc_info.value.kwargs == {"max_bytes": KNOWLEDGE_FILE_MAX_BYTES}
    assert source.read_sizes
    assert all(size == KNOWLEDGE_FILE_READ_CHUNK_BYTES for size in source.read_sizes)
