"""Migrate read-only backups of earlier local demo databases, preserving records."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from safeharness.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "artifacts/m4-migrations" / str(uuid4())
    directory.mkdir(parents=True)
    results = []
    for name in ["tasks.sqlite3", "m2-demo.sqlite3"]:
        original = ROOT / "artifacts" / name
        if not original.exists():
            continue
        destination = directory / name
        with sqlite3.connect(
            original.resolve().as_uri() + "?mode=ro", uri=True
        ) as source:
            old = source.execute("SELECT version_num FROM alembic_version").fetchone()[
                0
            ]
            before = source.execute(
                "SELECT task_id,status,revision FROM tasks ORDER BY task_id"
            ).fetchall()
            with sqlite3.connect(destination) as backup:
                source.backup(backup)
        TaskStore(destination).initialize()
        with sqlite3.connect(destination) as copied:
            after = copied.execute(
                "SELECT task_id,status,revision FROM tasks ORDER BY task_id"
            ).fetchall()
            head = copied.execute("SELECT version_num FROM alembic_version").fetchone()[
                0
            ]
        assert before == after and head == "0012_approval_loop"
        results.append(
            {
                "source": name,
                "old_version": old,
                "new_version": head,
                "preserved_tasks": len(before),
            }
        )
    (directory / "report.json").write_text(json.dumps(results, indent=2))
    print(
        json.dumps(
            {"report": str(directory / "report.json"), "results": results}, indent=2
        )
    )


if __name__ == "__main__":
    main()
