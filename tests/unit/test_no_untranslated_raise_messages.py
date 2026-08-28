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


def _raise_message_expressions(call: ast.Call) -> list[ast.expr]:
    expressions = [call.args[0]] if call.args else []
    expressions.extend(keyword.value for keyword in call.keywords if keyword.arg in {"message", "detail"})
    return expressions


def _find_untranslated_raise_messages(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue

        if not any(_is_literal_message(expression) for expression in _raise_message_expressions(node.exc)):
            continue

        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        expression_text = ast.get_source_segment(source, node.exc) or node.exc.__class__.__name__
        violations.append(f"{relative_path}:{node.lineno}: {expression_text}")

    return violations


def test_backend_raise_messages_are_connected_to_i18n():
    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        violations.extend(_find_untranslated_raise_messages(path))

    assert not violations, "以下报错文本未接入多语言，请改用错误常量或 t(...)：\n" + "\n".join(violations)


def test_raise_message_scan_ignores_structured_string_parameters():
    node = ast.parse('raise ExampleError(ERR_EXAMPLE, field="uid", maximum=50)').body[0]
    assert isinstance(node, ast.Raise)
    assert isinstance(node.exc, ast.Call)
    assert not any(_is_literal_message(expression) for expression in _raise_message_expressions(node.exc))


def test_raise_message_scan_still_detects_literal_messages():
    node = ast.parse('raise ExampleError(message="literal message", field="uid")').body[0]
    assert isinstance(node, ast.Raise)
    assert isinstance(node.exc, ast.Call)
    assert any(_is_literal_message(expression) for expression in _raise_message_expressions(node.exc))
