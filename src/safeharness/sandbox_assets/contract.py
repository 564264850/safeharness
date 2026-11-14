"""Small shared wire contract, containing no execution or host file access."""

import re

MAX_FILE_BYTES = 8192
MAX_TOTAL_BYTES = 32768
MAX_FILES = 32


class WorkspaceError(ValueError):
    pass


def validate_path(path):
    if not isinstance(path, str) or len(path) > 160 or not path:
        raise WorkspaceError("path_out_of_scope")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
        for part in path.split("/")
    ):
        raise WorkspaceError("path_out_of_scope")
    return path


def validate_files(files):
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise WorkspaceError("workspace_limit_exceeded")
    total = 0
    for path, text in files.items():
        validate_path(path)
        if not isinstance(text, str):
            raise WorkspaceError("invalid_file_content")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise WorkspaceError("file_limit_exceeded")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise WorkspaceError("workspace_limit_exceeded")
    return files
