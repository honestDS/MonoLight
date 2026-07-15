import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def _is_literal_message(expression: ast.expr) -> bool:
    if isinstance(expression, ast.Constant):
        return isinstance(expression.value, str)
    if isinstance(expression, ast.JoinedStr):
        return True
    if isinstance(expression, ast.BinOp):
        return _is_literal_message(expression.left) or _is_literal_message(expression.right)
    if isinstance(expression, ast.IfExp):
        return _is_literal_message(expression.body) or _is_literal_message(expression.orelse)
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute):
        return expression.func.attr == "format" and _is_literal_message(expression.func.value)
    return False


def _find_untranslated_raise_messages(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue

        expressions = [
            *node.exc.args,
            *(keyword.value for keyword in node.exc.keywords),
        ]
        if not any(_is_literal_message(expression) for expression in expressions):
            continue

        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        expression_text = ast.get_source_segment(source, node.exc) or node.exc.__class__.__name__
        violations.append(f"{relative_path}:{node.lineno}: {expression_text}")

    return violations


def test_backend_raise_messages_are_connected_to_i18n():
    violations = [
        violation
        for path in sorted(APP_ROOT.rglob("*.py"))
        for violation in _find_untranslated_raise_messages(path)
    ]

    assert not violations, (
        "以下报错文本未接入多语言，请改用错误常量或 t(...)：\n"
        + "\n".join(violations)
    )
