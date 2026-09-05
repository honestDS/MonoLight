import re
from dataclasses import dataclass

from app.core.constants import ERR_KB_CHUNK_OVERLAP_ERROR
from app.core.exceptions import ParameterException


@dataclass(frozen=True, slots=True)
class _SplitChunk:
    content: str
    start: int
    end: int


class TextSplitter:
    def __init__(self, chunk_size: int, chunk_overlap: int):
        if chunk_overlap >= chunk_size:
            raise ParameterException(ERR_KB_CHUNK_OVERLAP_ERROR)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        _, chunks = self._split_with_spans(text)
        return [chunk.content for chunk in chunks]

    def reconstruct_chunk_range(self, text: str, start_index: int, end_index: int) -> str | None:
        normalized_text, chunks = self._split_with_spans(text)
        if start_index < 0 or end_index < start_index or end_index >= len(chunks):
            return None
        return normalized_text[chunks[start_index].start : chunks[end_index].end]

    def _split_with_spans(self, text: str) -> tuple[str, list[_SplitChunk]]:
        normalized_text = text.replace("\r\n", "\n").strip()
        if not normalized_text:
            return "", []

        paragraphs: list[str] = []
        for raw_paragraph in re.split(r"\n\s*\n+", normalized_text):
            paragraph = raw_paragraph.strip()
            if paragraph:
                paragraphs.append(paragraph)

        normalized_text = "\n\n".join(paragraphs)
        paragraph_spans: list[tuple[str, int, int]] = []
        cursor = 0
        for paragraph in paragraphs:
            start = cursor
            end = start + len(paragraph)
            paragraph_spans.append((paragraph, start, end))
            cursor = end + 2

        chunks: list[_SplitChunk] = []
        current = ""
        current_start: int | None = None
        current_end: int | None = None

        for paragraph, paragraph_start, paragraph_end in paragraph_spans:
            if len(paragraph) > self.chunk_size:
                if current:
                    chunks.append(_SplitChunk(content=current.strip(), start=current_start or 0, end=current_end or 0))
                    current = ""
                    current_start = None
                    current_end = None
                chunks.extend(self._split_long_text_with_spans(paragraph, paragraph_start))
                continue

            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= self.chunk_size:
                if not current:
                    current_start = paragraph_start
                current = candidate
                current_end = paragraph_end
            else:
                if current:
                    chunks.append(_SplitChunk(content=current.strip(), start=current_start or 0, end=current_end or 0))
                current = paragraph
                current_start = paragraph_start
                current_end = paragraph_end

        if current:
            chunks.append(_SplitChunk(content=current.strip(), start=current_start or 0, end=current_end or 0))

        non_empty_chunks: list[_SplitChunk] = []
        for chunk in chunks:
            if chunk.content:
                non_empty_chunks.append(chunk)
        return normalized_text, non_empty_chunks

    def _split_long_text_with_spans(self, text: str, base_start: int) -> list[_SplitChunk]:
        chunks: list[_SplitChunk] = []
        start = 0
        text_length = len(text)
        step = self.chunk_size - self.chunk_overlap

        while start < text_length:
            hard_end = min(start + self.chunk_size, text_length)
            end = self._find_sentence_boundary(text, start, hard_end)
            raw_chunk = text[start:end]
            chunk = raw_chunk.strip()
            if chunk:
                leading_trim = len(raw_chunk) - len(raw_chunk.lstrip())
                trailing_trim = len(raw_chunk) - len(raw_chunk.rstrip())
                chunk_start = base_start + start + leading_trim
                chunk_end = base_start + end - trailing_trim
                chunks.append(_SplitChunk(content=chunk, start=chunk_start, end=chunk_end))
            if end >= text_length:
                break
            next_start = end - self.chunk_overlap
            start = next_start if next_start > start else start + step

        return chunks

    def _split_long_text(self, text: str) -> list[str]:
        return [chunk.content for chunk in self._split_long_text_with_spans(text, 0)]

    def _find_sentence_boundary(self, text: str, start: int, hard_end: int) -> int:
        if hard_end >= len(text):
            return hard_end

        boundary_chars = "。！？；.!?;\n"
        search_start = start + max(int(self.chunk_size * 0.6), 1)
        for index in range(hard_end - 1, search_start - 1, -1):
            if text[index] in boundary_chars:
                return index + 1
        return hard_end
