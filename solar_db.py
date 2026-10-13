"""Shared database connection helpers for the Solar scripts."""

from __future__ import annotations

import configparser
import os
import sqlite3
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"


def _database_config():
    config = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        config.read(CONFIG_FILE)
    return config


def database_backend():
    config = _database_config()
    configured = config.get("database", "backend", fallback="sqlite")
    return os.environ.get("SOLAR_DB_BACKEND", configured).strip().lower()


def sql_placeholder():
    return "%s" if database_backend() in {"postgres", "postgresql"} else "?"


def database_label(db_path=None):
    config = _database_config()
    backend = database_backend()
    if backend in {"postgres", "postgresql"}:
        name = os.environ.get(
            "SOLAR_DB_NAME", config.get("database", "name", fallback="solar_meter")
        )
        user = os.environ.get(
            "SOLAR_DB_USER", config.get("database", "user", fallback="solar_app")
        )
        return f"PostgreSQL database {name} (user {user})"

    path = Path(db_path or config.get("database", "path", fallback="solar_meter.db"))
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return f"SQLite database {path.expanduser()}"


def connect_database(db_path=None, *, named_rows=False, readonly=False):
    """Connect using [database] settings; SQLite remains available as fallback."""
    config = _database_config()
    database = config["database"] if config.has_section("database") else {}
    backend = os.environ.get(
        "SOLAR_DB_BACKEND", str(database.get("backend", "sqlite"))
    ).strip().lower()

    if backend in {"postgres", "postgresql"}:
        import psycopg
        from psycopg.rows import dict_row

        kwargs = {
            "dbname": os.environ.get(
                "SOLAR_DB_NAME", database.get("name", "solar_meter")
            ),
            "user": os.environ.get(
                "SOLAR_DB_USER", database.get("user", "solar_app")
            ),
        }
        host = os.environ.get("SOLAR_DB_HOST", database.get("host", "")).strip()
        if host:
            kwargs["host"] = host
        port = os.environ.get("SOLAR_DB_PORT", database.get("port", "")).strip()
        if port:
            kwargs["port"] = int(port)
        password = os.environ.get("SOLAR_DB_PASSWORD")
        if password:
            kwargs["password"] = password

        if named_rows:
            kwargs["row_factory"] = dict_row
        connection = psycopg.connect(**kwargs)
        if readonly:
            connection.execute("SET TRANSACTION READ ONLY")
        return connection

    path = Path(db_path or database.get("path", "solar_meter.db")).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")

    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    if named_rows:
        connection.row_factory = sqlite3.Row
    return connection
