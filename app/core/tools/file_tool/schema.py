FILE_TOOL_OPERATIONS = ("read", "write", "edit", "patch", "grep")


FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_tool",
        "description": (
            "Read, write, precisely edit, patch, or grep UTF-8 text files. Prefer edit for ordinary code changes, patch for several nearby or multi-hunk changes, and grep for regex search. edit new_text and patch added lines are literal text: backslash sequences such as \\n are never interpreted by the tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(FILE_TOOL_OPERATIONS),
                    "description": "File operation: read, write, edit, patch, or grep.",
                },
                "path": {
                    "type": "string",
                    "description": ("Target UTF-8 file path. grep may also receive a directory. Relative paths stay inside the user workspace; absolute paths must be inside an allowed operation directory."),
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "read only. Inclusive 1-based start line; defaults to 1.",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "read only. Inclusive 1-based end line; defaults to a bounded page after start_line.",
                },
                "content": {
                    "type": "string",
                    "description": "write only. Complete literal file content; write fully overwrites the target.",
                },
                "old_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": ("edit only. Literal text to replace. Include enough surrounding text to make the target unique. Line endings are normalized to the target file before matching."),
                },
                "new_text": {
                    "type": "string",
                    "description": ("edit only. Literal replacement text. The tool does not interpret escapes or regex replacement syntax; for example \\n remains a backslash followed by n in the file unless the JSON value contains an actual newline."),
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "edit only. Replace every literal old_text match. Default false requires exactly one match.",
                },
                "patch": {
                    "type": "string",
                    "description": (
                        "patch only. One or more unified-diff-style hunks for this file. Start each hunk with @@; "
                        "prefix unchanged context with a space, removed lines with -, and added lines with +. "
                        "Do not include file headers or line-number ranges. Added text is literal and backslashes are not interpreted. "
                        "All hunks must locate uniquely before the file is written."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": "grep only. Python regular expression used for search; never used for file modification.",
                },
                "recursive": {
                    "type": "boolean",
                    "default": True,
                    "description": "grep only. Recurse into subdirectories when path is a directory.",
                },
                "include": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "grep only. Optional glob patterns such as *.py or src/**/*.vue.",
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "grep only. Optional glob patterns to exclude.",
                },
                "ignore_case": {
                    "type": "boolean",
                    "default": False,
                    "description": "grep only. Perform case-insensitive regex matching.",
                },
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 2,
                    "description": "grep only. Number of surrounding lines returned around each match.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                    "description": "grep only. Maximum number of matches returned before truncated=true.",
                },
            },
            "required": ["operation", "path"],
            "additionalProperties": False,
        },
    },
}
