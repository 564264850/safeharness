"""Reproducible local infrastructure; credentials are stored in ignored files."""

import json
import secrets
import subprocess
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from .database import engine_for, normalize_database

POSTGRES_IMAGE = (
    "postgres@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac"
)


def postgres_url(directory: Path):
    return normalize_database(
        json.loads((directory / "postgres.json").read_text())["database_url"]
    )


def postgres_up(directory: Path, *, port=15433):
    directory.mkdir(parents=True, exist_ok=True)
    settings = directory / "postgres.json"
    if not settings.exists():
        password = secrets.token_urlsafe(32)
        url = URL.create(
            "postgresql+psycopg",
            username="safeharness",
            password=password,
            host="127.0.0.1",
            port=port,
            database="safeharness",
        )
        settings.write_text(
            json.dumps({"database_url": url.render_as_string(hide_password=False)})
        )
        (directory / "postgres.env").write_text(
            "POSTGRES_USER=safeharness\nPOSTGRES_DB=safeharness\nPOSTGRES_PASSWORD="
            + password
            + "\n"
        )
        settings.chmod(0o600)
        (directory / "postgres.env").chmod(0o600)
    inspected = subprocess.run(
        ["docker", "inspect", "safeharness-postgres-m4"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if inspected.returncode:
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            "safeharness-postgres-m4",
            "--label",
            "safeharness.managed=m4-postgres",
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env-file",
            str((directory / "postgres.env").resolve()),
            "--mount",
            "type=volume,src=safeharness-pgdata-m4,dst=/var/lib/postgresql/data",
            "--memory=512m",
            "--cpus=1",
            "--pids-limit=128",
            POSTGRES_IMAGE,
        ]
    else:
        metadata = json.loads(inspected.stdout)[0]
        if metadata["Config"]["Labels"].get("safeharness.managed") != "m4-postgres":
            raise ValueError("Container name is owned by another project")
        command = ["docker", "start", "safeharness-postgres-m4"]
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise ValueError("Could not start the dedicated PostgreSQL container")
    engine = engine_for(postgres_url(directory))
    deadline = time.monotonic() + 20
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            break
        except SQLAlchemyError:
            if time.monotonic() >= deadline:
                raise ValueError("PostgreSQL readiness timed out") from None
            time.sleep(0.25)
    return {
        "container": "safeharness-postgres-m4",
        "port": port,
        "image": POSTGRES_IMAGE,
        "config_file": str(settings),
    }
