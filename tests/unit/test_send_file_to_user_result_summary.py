import json
from collections.abc import Callable

import pytest

from app.core.constants import (
    ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS,
    ERR_FILE_SEND_DIRS_UNCONFIGURED,
    MSG_TOOL_FILE_SEND_FAILED,
    MSG_TOOL_FILE_SEND_SUCCESS,
)
from app.core.i18n import t
from app.core.tools.send_file_to_user import (
    sanitize_files_to_user_result,
    summarize_files_to_user_result,
)


@pytest.fixture(params=[summarize_files_to_user_result, sanitize_files_to_user_result])
def summarize_result(request: pytest.FixtureRequest) -> Callable[[str | None], str | None]:
    return request.param


def test_unconfigured_allowed_dirs_error_is_preserved(summarize_result: Callable[[str | None], str | None]) -> None:
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [],
            "errors": [{"path": "", "error": t(ERR_FILE_SEND_DIRS_UNCONFIGURED)}],
            "allowed_file_send_dirs": [],
        },
        ensure_ascii=False,
    )

    result = json.loads(summarize_result(content))

    assert result == {
        "type": "files_to_user_result",
        "status": "failed",
        "message": t(ERR_FILE_SEND_DIRS_UNCONFIGURED),
    }


def test_path_error_does_not_leak_path_or_allowed_dirs(summarize_result: Callable[[str | None], str | None]) -> None:
    path = "/private/reports/secret.pdf"
    allowed_dir = "/private/reports"
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [],
            "errors": [{"path": path, "error": t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS)}],
            "allowed_file_send_dirs": [allowed_dir],
        },
        ensure_ascii=False,
    )

    result_content = summarize_result(content)
    result = json.loads(result_content)

    assert result["message"] == t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS)
    assert path not in result_content
    assert allowed_dir not in result_content


def test_duplicate_errors_are_stripped_and_deduplicated(summarize_result: Callable[[str | None], str | None]) -> None:
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [],
            "errors": [
                {"error": " first error "},
                {"error": "first error"},
                {"error": "second error"},
                {"error": " second error "},
            ],
        }
    )

    result = json.loads(summarize_result(content))

    assert result["message"] == "first error\nsecond error"


def test_missing_valid_error_falls_back_to_generic_failure_message(summarize_result: Callable[[str | None], str | None]) -> None:
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [],
            "errors": [
                {"path": "/private/reports/secret.pdf"},
                {"error": None},
                {"error": "   "},
                {"error": 1},
                "not an error object",
            ],
        }
    )

    result = json.loads(summarize_result(content))

    assert result["message"] == t(MSG_TOOL_FILE_SEND_FAILED)


def test_success_does_not_leak_file_token_url_or_allowed_dirs(summarize_result: Callable[[str | None], str | None]) -> None:
    token = "sensitive-download-token"
    download_url = f"/api/v1/download-sent?token={token}"
    allowed_dir = "/private/reports"
    content = json.dumps(
        {
            "type": "files_to_user",
            "files": [{"id": token, "download_url": download_url}],
            "errors": [],
            "allowed_file_send_dirs": [allowed_dir],
        }
    )

    result_content = summarize_result(content)
    result = json.loads(result_content)

    assert result == {
        "type": "files_to_user_result",
        "status": "success",
        "message": t(MSG_TOOL_FILE_SEND_SUCCESS),
    }
    assert token not in result_content
    assert download_url not in result_content
    assert allowed_dir not in result_content
