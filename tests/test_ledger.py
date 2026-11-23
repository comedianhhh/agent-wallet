"""The database itself refuses a broken ledger, even if the Python code had a bug or someone runs SQL by hand."""

from __future__ import annotations

import psycopg
import pytest

from agent_wallet import wallet


def test_unbalanced_entry_is_rejected_at_commit(world):
    c = world["conn"]
    w = world["user"]["wallet_id"]
    with pytest.raises(psycopg.errors.CheckViolation, match="does not balance"):
        with c.transaction():
            e = c.execute("INSERT INTO journal_entries (kind) VALUES ('fund') RETURNING id").fetchone()["id"]
            c.execute("INSERT INTO postings (entry_id, account_id, amount_cents) VALUES (%s, %s, 1000000)", (e, w))
    assert wallet.balance(c, w) == 50_000


def test_wallet_cannot_go_negative_even_with_raw_sql(world):
    c = world["conn"]
    w = world["user"]["wallet_id"]
    m = c.execute("SELECT account_id FROM merchants WHERE id = 'attacker'").fetchone()["account_id"]
    with pytest.raises(psycopg.errors.CheckViolation, match="negative"):
        with c.transaction():
            e = c.execute("INSERT INTO journal_entries (kind) VALUES ('payment') RETURNING id").fetchone()["id"]
            c.execute(
                "INSERT INTO postings (entry_id, account_id, amount_cents) VALUES (%s, %s, -60000), (%s, %s, 60000)",
                (e, w, e, m),
            )
    assert wallet.balance(c, w) == 50_000


@pytest.mark.parametrize("sql", [
    "UPDATE postings SET amount_cents = amount_cents * 2",
    "DELETE FROM postings",
    "UPDATE journal_entries SET kind = 'refund'",
    "DELETE FROM journal_entries",
])
def test_ledger_is_append_only(world, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        world["conn"].execute(sql)


def test_verify_ledger_catches_a_payment_row_that_lies(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "coffee", 1_000, "x")
    assert wallet.verify_ledger(c)["ok"]
    c.execute("UPDATE payments SET amount_cents = 1 WHERE id = %s", (p["payment_id"],))
    r = wallet.verify_ledger(c)
    assert not r["ok"] and r["payments_not_matching_ledger"] == [p["payment_id"]]
