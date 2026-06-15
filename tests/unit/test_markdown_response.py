from app.core.utils.dispatcher.process_markdown_response import process_markdown_response, remove_markdown
from app.models.message import InternalMessage, InternalToolCall, MessageRole


def test_remove_markdown_strips_common_markdown_marks():
    text = "# 标题\n\n这是 **加粗** 和 `代码`。"

    assert remove_markdown(text) == "标题\n\n这是 加粗 和 代码。"


def test_remove_markdown_handles_fenced_code_block_with_language_and_comment():
    text = """这里是示例：

```python
# 这是一行 Python 注释
print("hello")
```

结束。"""

    assert remove_markdown(text) == '这里是示例：\n\n# 这是一行 Python 注释\nprint("hello")\n\n结束。'


def test_process_markdown_response_disable_markdown():
    msg = InternalMessage(role=MessageRole.ASSISTANT, content="# 标题\n\n**内容**")

    processed = process_markdown_response(msg, enable_markdown=False)

    assert processed.content == "标题\n\n内容"


def test_process_markdown_response_enable_markdown():
    content = "# 标题\n\n**内容**"
    msg = InternalMessage(role=MessageRole.ASSISTANT, content=content)

    processed = process_markdown_response(msg, enable_markdown=True)

    assert processed.content == content


def test_process_markdown_response_skip_tool_calls():
    content = "# 标题\n\n**内容**"
    msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=content,
        tool_calls=[InternalToolCall(id="call_1", name="test_tool", arguments={})],
    )

    processed = process_markdown_response(msg, enable_markdown=False)

    assert processed.content == content
