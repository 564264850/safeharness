"""BudgetBook 持久化辅助：带版本与 next_id 的 JSON 文件读写。"""

import json
import os
import tempfile
from pathlib import Path

VERSION = 1


def load(path):
    """返回 (entries, next_id)。文件不存在视为空账本。"""
    path = Path(path)
    if not path.exists():
        return [], 1
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取数据文件: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("数据文件已损坏，无法解析 JSON") from exc
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError("数据文件结构不兼容")
    entries = data.get("entries")
    next_id = data.get("next_id")
    if (
        not isinstance(entries, list)
        or not isinstance(next_id, int)
        or isinstance(next_id, bool)
    ):
        raise ValueError("数据文件结构不兼容")  # noqa: TRY004 - Storage errors share ValueError.
    clean = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("数据文件结构不兼容")  # noqa: TRY004 - Storage errors share ValueError.
        try:
            clean.append(
                {
                    "id": int(item["id"]),
                    "amount": str(item["amount"]),
                    "category": str(item["category"]),
                    "note": str(item["note"]),
                    "date": str(item["date"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("数据文件结构不兼容") from exc
    if next_id < 1:
        raise ValueError("数据文件结构不兼容")
    return clean, next_id


def save(path, entries, next_id):
    """先写同目录临时文件，再 os.replace 原子替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": VERSION, "next_id": next_id, "entries": entries},
        ensure_ascii=False,
        indent=1,
    )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".book-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
