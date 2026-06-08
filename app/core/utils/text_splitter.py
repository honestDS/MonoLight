import re


class TextSplitter:
    def __init__(self, chunk_size: int, chunk_overlap: int):
        if chunk_overlap >= chunk_size:
            raise ValueError("分块重叠必须小于分块大小")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        normalized_text = text.replace("\r\n", "\n").strip()
        if not normalized_text:
            return []

        paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", normalized_text) if item.strip()]
        chunks: list[str] = []
        current = ""

        for paragraph in paragraphs:
            if len(paragraph) > self.chunk_size:
                if current:
                    chunks.append(current.strip())
                    current = ""
                chunks.extend(self._split_long_text(paragraph))
                continue

            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= self.chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                current = paragraph

        if current:
            chunks.append(current.strip())

        return [chunk for chunk in chunks if chunk]

    def _split_long_text(self, text: str) -> list[str]:
        chunks: list[str] = []
        start = 0
        text_length = len(text)
        step = self.chunk_size - self.chunk_overlap

        while start < text_length:
            hard_end = min(start + self.chunk_size, text_length)
            end = self._find_sentence_boundary(text, start, hard_end)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= text_length:
                break
            next_start = end - self.chunk_overlap
            start = next_start if next_start > start else start + step

        return chunks

    def _find_sentence_boundary(self, text: str, start: int, hard_end: int) -> int:
        if hard_end >= len(text):
            return hard_end

        boundary_chars = "。！？；.!?;\n"
        search_start = start + max(int(self.chunk_size * 0.6), 1)
        for index in range(hard_end - 1, search_start - 1, -1):
            if text[index] in boundary_chars:
                return index + 1
        return hard_end
