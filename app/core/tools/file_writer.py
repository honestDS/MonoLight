import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.constants import ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.utils.system import get_full_system_context

from .base import BaseExecutor


def resolve_file_writer_target_path(file_path: str, workspace_path: str | Path) -> Path:
    if not file_path or PurePosixPath(file_path).is_absolute() or PureWindowsPath(file_path).is_absolute():
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))

    workspace_path = Path(workspace_path).resolve(strict=False)
    target_path = (workspace_path / file_path).resolve(strict=False)
    if target_path == workspace_path or not target_path.is_relative_to(workspace_path):
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))
    return target_path


class FileWriterExecutor(BaseExecutor):
    logger = get_logger(__name__)

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = get_user_temp_dir(self.project_root, uid)
        self._ensure_temp_dir()

    def _ensure_temp_dir(self):
        if not self.user_temp_dir.exists():
            self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_target_path(self, file_path: str) -> Path:
        return resolve_file_writer_target_path(file_path, self.user_temp_dir)

    async def execute(self, file_path: str, content: str, append: bool = False) -> str:
        """
        将内容写入指定文件。

        :param file_path: 相对路径（相对于用户临时目录）
        :param content: 要写入的文本内容
        :param append: 是否以追加模式写入，默认为 False (覆盖)
        """
        system_info = get_full_system_context()
        try:
            target_path = self._resolve_target_path(file_path)

            # 确保父目录存在
            target_path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            encoding = "utf-8"

            def write_sync():
                with open(target_path, mode, encoding=encoding) as f:
                    f.write(content)

            await self.run_sync(write_sync)

            self.logger.info(f"[{self.uid}] File {'appended' if append else 'written'}: {file_path}")

            return json.dumps({"status": "success", "file_path": file_path, "bytes_written": len(content.encode(encoding)), "mode": "append" if append else "write", "system_info": system_info}, ensure_ascii=False)

        except Exception as e:
            self.logger.error(f"[{self.uid}] Failed to write file {file_path}: {e}")
            return json.dumps({"error": str(e), "system_info": system_info}, ensure_ascii=False)


FILE_WRITER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write multi-line text to a file (e.g., scripts, configs, data).",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The relative path to the file within the workspace (e.g., 'script.py', 'data/config.json').",
                },
                "content": {
                    "type": "string",
                    "description": "The multi-line content to be written to the file.",
                },
                "append": {"type": "boolean", "description": "If true, appends content to the end of the file. If false (default), overwrites the file.", "default": False},
            },
            "required": ["file_path", "content"],
        },
    },
}
