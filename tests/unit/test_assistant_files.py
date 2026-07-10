import json

from app.adapters.weixin_openclaw.response import extract_event_reply, extract_reply_files, extract_reply_text
from app.core.utils.assistant_files import build_assistant_files_content, merge_assistant_files, parse_assistant_files_content


def test_assistant_files_content_only_restores_text():
    files = [{"id": "file-1", "name": "generated.png"}]

    content = build_assistant_files_content("图片已发送。", files)
    text, parsed_files = parse_assistant_files_content(content)

    assert json.loads(content)["type"] == "assistant_files"
    assert text == "图片已发送。"
    assert parsed_files == []


def test_plain_text_content_is_not_treated_as_file_protocol():
    text, files = parse_assistant_files_content("普通回复")

    assert text == "普通回复"
    assert files == []


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
