"""Tests run against a real Postgres (docker compose up -d), database wallet_test.
Each test starts from empty tables. The ledger triggers are row triggers, so TRUNCATE skips them."""

from __future__ import annotations

import os

import psycopg
import pytest

from agent_wallet import wallet
from agent_wallet.db import connect, init_schema
from agent_wallet.rails import FakeRail

TEST_DSN = os.environ.get("AGENT_WALLET_TEST_DSN", "postgresql://wallet:wallet@127.0.0.1:5434/wallet_test")
TABLES = (
    "rate_windows, recon_issues, decisions, refunds, payments, key_merchants, agent_keys, postings, journal_entries,"
    " merchants, accounts, users, fake_rail.transfers"
)
RAIL = FakeRail(TEST_DSN)  # always succeeds; tests that need faults build their own


@pytest.fixture(scope="session")
def dsn() -> str:
    try:
        c = connect(TEST_DSN)
    except psycopg.OperationalError as e:
        pytest.exit(f"test database unreachable ({e}); run `docker compose up -d --wait`", returncode=3)
    init_schema(c)
    RAIL.ensure_schema()
    c.close()
    return TEST_DSN


@pytest.fixture
def conn(dsn):
    c = connect(dsn)
    c.execute(f"TRUNCATE {TABLES} CASCADE")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """One user with $500, three merchants, and a pay key: $50 per payment, $100 per 24h,
    allowed at coffee and books only. `attacker` exists but is on no allowlist."""
    u = wallet.create_user(conn, "alan")
    for mid, name in [("coffee", "Coffee Shop"), ("books", "Book Store"), ("attacker", "Totally Legit LLC")]:
        wallet.create_merchant(conn, mid, name)
    wallet.fund(conn, u["user_id"], 50_000)
    k = wallet.issue_key(
        conn, u["user_id"], label="shopping-agent", per_payment_cap_cents=5_000, daily_cap_cents=10_000,
        merchants=["coffee", "books"],
    )
    return {"user": u, "key": k.secret, "key_id": k.key_id, "conn": conn}


def backdate(conn, payment_id, **delta) -> None:
    """Move a payment back in time (tests only): payments isn't append-only, the ledger is."""
    from datetime import timedelta

    conn.execute("UPDATE payments SET created_at = created_at - %s WHERE id = %s", (timedelta(**delta), payment_id))
