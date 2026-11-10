import json
from pathlib import Path
from typing import Any

from app.core.constants import (
    ERR_FILE_TOOL_NOT_REGULAR,
    ERR_FILE_TOOL_OPERATION_INVALID,
    ERR_TOOL_WRITE_PROTECTED_DIRECTORY,
    FILE_TOOL_GREP_MAX_OUTPUT_BYTES,
)
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.utils.operation_directories import get_allowed_operation_dirs, is_path_write_protected

from ..base import BaseExecutor
from .edit import edit_file
from .grep import grep_files
from .patch import patch_file
from .paths import resolve_file_tool_target_path
from .read import read_file
from .schema import FILE_TOOL_OPERATIONS
from .write import write_file


class FileToolExecutor(BaseExecutor):
    requires_audit = True

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = get_user_temp_dir(self.project_root, uid)
        self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_target_path(self, path: str, *, enforce_allowed_dirs: bool = True):
        return resolve_file_tool_target_path(
            path,
            self.user_temp_dir,
            get_allowed_operation_dirs(self.cfg),
            enforce_allowed_dirs=enforce_allowed_dirs,
        )

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        if payload.get("operation") != "grep" or payload.get("status") != "failed":
            return json.dumps(payload, ensure_ascii=False)

        error = payload["error"]
        if len(error) <= FILE_TOOL_GREP_MAX_OUTPUT_BYTES:
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized.encode("utf-8")) <= FILE_TOOL_GREP_MAX_OUTPUT_BYTES:
                return serialized

        truncated_payload = {**payload, "error": "", "error_truncated": True}
        shell_bytes = len(json.dumps(truncated_payload, ensure_ascii=False).encode("utf-8"))
        # Escaped JSON characters can use up to six UTF-8 bytes each.
        max_error_chars = max(0, (FILE_TOOL_GREP_MAX_OUTPUT_BYTES - shell_bytes) // 6)
        truncated_payload["error"] = error[:max_error_chars]
        return json.dumps(truncated_payload, ensure_ascii=False)

    @staticmethod
    def _failed(operation: str, error: str, **details: Any) -> str:
        return FileToolExecutor._dump(
            {
                "status": "failed",
                "operation": operation,
                "error": error,
                **details,
            }
        )

    async def execute(
        self,
        operation: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        content: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
        patch: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        ignore_case: bool = False,
        context_lines: int = 2,
        max_results: int = 100,
        before_chars: int | None = None,
        after_chars: int | None = None,
    ) -> str:
        if operation not in FILE_TOOL_OPERATIONS:
            return self._failed(str(operation), t(ERR_FILE_TOOL_OPERATION_INVALID, operation=operation))

        try:
            target = await self.run_sync(
                self._resolve_target_path,
                path,
                enforce_allowed_dirs=operation not in {"read", "grep"},
            )
            if operation in {"write", "edit", "patch"} and getattr(getattr(self.cfg, "security", None), "protect_system_directories", True):
                protection_path = Path(path)
                if not protection_path.is_absolute():
                    protection_path = await self.run_sync(self.user_temp_dir.resolve) / protection_path
                if await self.run_sync(is_path_write_protected, protection_path, self.project_root):
                    return self._dump(
                        {
                            "status": "blocked",
                            "operation": operation,
                            "path": str(target),
                            "error": t(ERR_TOOL_WRITE_PROTECTED_DIRECTORY, path=str(target)),
                        }
                    )
            if operation == "read":
                result = await self.run_sync(read_file, target, start_line=start_line, end_line=end_line)
            elif operation == "write":
                result = await self.run_sync(write_file, target, content=content)
            elif operation == "edit":
                result = await self.run_sync(
                    edit_file,
                    target,
                    old_text=old_text,
                    new_text=new_text,
                    replace_all=replace_all,
                )
            elif operation == "patch":
                result = await self.run_sync(patch_file, target, patch=patch)
            else:
                result = await self.run_sync(
                    grep_files,
                    target,
                    pattern=pattern,
                    recursive=recursive,
                    include=include,
                    exclude=exclude,
                    ignore_case=ignore_case,
                    context_lines=context_lines,
                    max_results=max_results,
                    before_chars=before_chars,
                    after_chars=after_chars,
                )
            return self._dump(result)
        except UnicodeDecodeError:
            return self._failed(operation, t(ERR_FILE_TOOL_NOT_REGULAR))
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return self._failed(operation, str(exc))
