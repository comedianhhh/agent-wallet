"""Postgres connection and schema setup."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DSN = "postgresql://wallet:wallet@127.0.0.1:5434/wallet"


def dsn() -> str:
    return os.environ.get("AGENT_WALLET_DSN", DEFAULT_DSN)


def load_env_file(path: str) -> None:
    """KEY=VALUE lines; blank lines and # comments skipped; variables already set win."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def connect(url: str | None = None) -> psycopg.Connection:
    """A connection in autocommit mode: every unit of work opens its own `conn.transaction()`."""
    return psycopg.connect(url or dsn(), autocommit=True, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(4207001)")  # two processes starting at once
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
