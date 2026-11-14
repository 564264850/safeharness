"""Small dialect boundary shared by state, audit and Trace persistence."""

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.pool import NullPool

Database = Path | URL


def normalize_database(value: Path | str | URL) -> Database:
    if isinstance(value, URL):
        result = value
    elif isinstance(value, str) and value.startswith("postgresql"):
        result = make_url(value)
    else:
        return Path(value).resolve()
    if result.drivername != "postgresql+psycopg":
        raise ValueError("Use the explicit postgresql+psycopg driver")
    return result


def database_exists(database: Database):
    return database.exists() if isinstance(database, Path) else True


def engine_for(
    database: Database,
    *,
    statement_timeout_ms=15000,
    connect_timeout_seconds=5,
    sqlite_timeout_seconds=5,
):
    url = (
        URL.create("sqlite", database=str(database))
        if isinstance(database, Path)
        else database
    )
    engine = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": sqlite_timeout_seconds}
        if isinstance(database, Path)
        else {
            "connect_timeout": connect_timeout_seconds,
            "options": (
                url.query.get("options", "")
                + f" -c statement_timeout={int(statement_timeout_ms)}"
            ).strip(),
        },
    )
    if isinstance(database, Path):

        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON").close()

    return engine


def execute(connection, query: str, parameters=()):
    if not isinstance(connection, Connection):
        return connection.execute(query, parameters)
    parts = query.split("?")
    if len(parts) - 1 != len(parameters):
        raise ValueError("SQL placeholder mismatch")
    statement = parts[0] + "".join(
        f":p{index}" + part for index, part in enumerate(parts[1:])
    )
    return connection.execute(
        text(statement), {f"p{index}": value for index, value in enumerate(parameters)}
    )


def lock_task(connection, task_id):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        execute(
            connection,
            "SELECT task_id FROM tasks WHERE task_id=? FOR UPDATE",
            (task_id.hex,),
        ).fetchone()
