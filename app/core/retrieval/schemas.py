from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalChunk:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalHit:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    dense_distance: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None
    fusion_score: float | None = None
