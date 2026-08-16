import json

import pytest

from app.adapters.weixin_openclaw.response import extract_event_reply, extract_reply_files, extract_reply_text
from app.core.utils.assistant_files import build_assistant_files_content, merge_assistant_files, parse_assistant_files_content
from app.core.utils.dispatcher.process_markdown_response import process_markdown_response
from app.models.message import InternalMessage, MessageRole


def test_assistant_files_content_only_restores_text():
    files = [{"id": "file-1", "name": "generated.png"}]

    content = build_assistant_files_content("图片已发送。", files)
    text = parse_assistant_files_content(content)

    assert json.loads(content)["type"] == "assistant_files"
    assert text == "图片已发送。"


def test_plain_text_content_is_not_treated_as_file_protocol():
    text = parse_assistant_files_content("普通回复")

    assert text == "普通回复"


def test_build_assistant_files_content_unwraps_existing_protocol():
    old_file = {"id": "old-file", "name": "old.png"}
    current_file = {"id": "current-file", "name": "current.png"}
    nested_content = build_assistant_files_content("图片已重新发送。", [old_file])

    content = build_assistant_files_content(nested_content, [current_file])
    payload = json.loads(content)

    assert payload == {
        "type": "assistant_files",
        "text": "图片已重新发送。",
        "files": [current_file],
    }


@pytest.mark.parametrize(
    ("enable_markdown", "files", "expected_text"),
    [
        (False, [{"id": "file-1", "name": "generated.png"}], "图片已发送。\n\n结果一"),
        (True, [{"id": "file-1"}], "**图片**已发送。\n\n- 结果一"),
    ],
)
def test_markdown_processing_inside_assistant_files_protocol(enable_markdown, files, expected_text):
    markdown_text = "**图片**已发送。\n\n- 结果一"
    content = build_assistant_files_content(markdown_text, files)
    message = InternalMessage(role=MessageRole.ASSISTANT, content=content)

    processed = process_markdown_response(message, enable_markdown=enable_markdown)
    payload = json.loads(processed.content)

    if enable_markdown:
        assert processed.content == content

    assert payload["type"] == "assistant_files"
    assert payload["text"] == expected_text
    assert payload["files"] == files


def test_merge_assistant_files_deduplicates_by_id_and_path():
    first = {"id": "file-1", "name": "first.png"}
    duplicate = {"id": "file-1", "name": "duplicate.png"}
    path_only = {"path": "D:/temp/second.png", "name": "second.png"}

    files = merge_assistant_files([first], [duplicate, path_only], [path_only])

    assert files == [first, path_only]


def test_weixin_event_only_trusts_structured_event_files():
    structured_file = {"id": "file-1", "name": "new.png"}
    legacy_file = {"id": "file-2", "name": "legacy.png"}
    event = {
        "content": "图片已发送。",
        "files": [structured_file],
        "history": [
            {
                "role": "assistant",
                "content": build_assistant_files_content("旧格式回复", [structured_file, legacy_file]),
            }
        ],
    }

    text, files = extract_event_reply(event)

    assert text == "图片已发送。"
    assert files == [structured_file]


def test_weixin_event_rejects_files_from_legacy_content():
    legacy_file = {"id": "file-1", "name": "legacy.png"}
    event = {
        "content": build_assistant_files_content("旧格式回复", [legacy_file]),
    }

    text, files = extract_event_reply(event)

    assert text == "旧格式回复"
    assert files == []


def test_weixin_llm_response_only_trusts_top_level_files():
    top_level_file = {"id": "file-1", "name": "new.png"}
    legacy_file = {"id": "file-2", "name": "legacy.png"}
    response = {
        "files": [top_level_file],
        "choices": [
            {
                "message": {
                    "content": build_assistant_files_content("回复文本", [top_level_file, legacy_file]),
                }
            }
        ],
    }

    assert extract_reply_text(response) == "回复文本"
    assert extract_reply_files(response) == [top_level_file]
