from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy.exc import IntegrityError


def integrity_error_detail(exc: IntegrityError) -> tuple[str, str]:
    """提取并标准化完整性异常中的约束名与数据库原始错误详情。"""
    original = getattr(exc, "orig", None)
    constraint_name = str(
        getattr(original, "constraint_name", None)
        or getattr(exc, "constraint_name", None)
        or ""
    ).lower()
    detail = " ".join(part.lower() for part in (str(original or ""), str(exc)))
    return constraint_name, detail


def is_unique_constraint_violation(
    exc: IntegrityError,
    *,
    constraint_names: Iterable[str] = (),
    fallback_marker_groups: Sequence[Sequence[str]] = (),
) -> bool:
    """优先按稳定约束名识别唯一键冲突，并兼容 SQLite 等方言的文本错误格式。"""
    constraint_name, detail = integrity_error_detail(exc)
    normalized_constraints = tuple(name.lower() for name in constraint_names if name)
    if any(name == constraint_name or name in detail for name in normalized_constraints):
        return True

    if not any(marker in detail for marker in ("unique", "duplicate key", "duplicate entry")):
        return False
    return any(
        all(marker.lower() in detail for marker in marker_group)
        for marker_group in fallback_marker_groups
        if marker_group
    )


__all__ = ["integrity_error_detail", "is_unique_constraint_violation"]
