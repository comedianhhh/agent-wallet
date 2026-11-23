"""The rail: declines, timeouts with unknown outcome, crashes, many workers, reconciliation, chaos."""

from __future__ import annotations

import random
import threading
from collections import Counter

import pytest

from agent_wallet import settlement, wallet
from agent_wallet.db import connect
from agent_wallet.rails import FakeRail

from .conftest import RAIL, TEST_DSN


def faulty(*modes):
    """A rail whose n-th call gets modes[n] (then 'ok' forever)."""
    calls = iter(modes)
    return FakeRail(TEST_DSN, faults=lambda _key: next(calls, "ok"))


def make_due(conn):
    """Skip the backoff wait (tests only)."""
    conn.execute("UPDATE payments SET next_attempt_at = now() - interval '1 second' WHERE status = 'processing'")


def rail_rows(conn):
    return conn.execute("SELECT * FROM fake_rail.transfers").fetchall()


def held(w):
    return wallet.wallet_view(w["conn"], w["key"])["held_cents"]


def bal(w):
    return wallet.balance(w["conn"], w["user"]["wallet_id"])


def test_happy_path_holds_then_captures(world):
    p = wallet.pay(world["conn"], world["key"], "coffee", 1_250, "a", rail=RAIL)
    assert p["status"] == "settled"
    assert bal(world) == 48_750 and held(world) == 0
    assert len(rail_rows(world["conn"])) == 1
    assert settlement.reconcile(world["conn"], RAIL)["ok"]


def test_rail_decline_returns_the_money_and_the_budget(world):
    c, k = world["conn"], world["key"]
    p = wallet.pay(c, k, "coffee", 5_000, "a", rail=faulty("decline"))
    assert p["status"] == "failed" and p["failure_reason"] == "merchant declined"
    assert bal(world) == 50_000 and held(world) == 0
    assert wallet.wallet_view(c, k)["used_last_24h_cents"] == 0
    assert wallet.verify_ledger(c)["ok"]


def test_response_lost_after_rail_paid(world):
    """The dangerous one: the merchant got the money, we got a timeout. Must not release the hold,
    must not pay twice, must end settled."""
    c, k = world["conn"], world["key"]
    p = wallet.pay(c, k, "coffee", 3_000, "a", rail=faulty("timeout_after"))
    assert p["status"] == "processing"
    assert bal(world) == 47_000 and held(world) == 3_000  # still held, not released
    make_due(c)
    assert settlement.run_worker_once(c, RAIL)["settled"] == 1
    assert wallet.get_payment(c, k, p["payment_id"])["status"] == "settled"
    assert len(rail_rows(c)) == 1  # the retry did not make a second transfer
    assert bal(world) == 47_000 and held(world) == 0
    assert settlement.reconcile(c, RAIL)["ok"] and wallet.verify_ledger(c)["ok"]


def test_request_never_reached_the_rail(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "coffee", 3_000, "a", rail=faulty("timeout_before", "timeout_before"))
    assert p["status"] == "processing" and rail_rows(c) == []
    make_due(c)
    r = settlement.run_worker_once(c, faulty("timeout_before"))
    assert r["still_unknown"] == 1
    make_due(c)
    assert settlement.run_worker_once(c, RAIL)["settled"] == 1
    assert len(rail_rows(c)) == 1


def test_crash_between_commit_and_rail_call(world):
    """pay() authorized and committed, then the process died before calling the rail (rail=None
    stands in for the crash). The worker finds it and finishes it."""
    c = world["conn"]
    p = wallet.pay(c, world["key"], "coffee", 2_000, "a")
    assert p["status"] == "processing" and held(world) == 2_000
    assert settlement.run_worker_once(c, RAIL)["settled"] == 1
    assert bal(world) == 48_000 and held(world) == 0


def test_lease_stops_two_workers_sending_the_same_payment(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "coffee", 2_000, "a")
    from uuid import UUID

    assert settlement._claim(c, UUID(p["payment_id"])) is not None
    assert settlement._claim(c, UUID(p["payment_id"])) is None  # leased to the first one


def test_many_workers_at_once_pay_each_payment_once(dsn, conn):
    u = wallet.create_user(conn, "busy")
    wallet.create_merchant(conn, "shop", "Shop")
    wallet.fund(conn, u["user_id"], 1_000_000)
    k = wallet.issue_key(conn, u["user_id"], label="bulk", per_payment_cap_cents=10_000,
                         daily_cap_cents=10_000_000, merchants=["shop"]).secret
    for i in range(40):
        wallet.pay(conn, k, "shop", 100 + i, f"p{i}")  # authorized only: a backlog for the workers

    barrier = threading.Barrier(6)

    def work():
        c = connect(dsn)
        barrier.wait()
        for _ in range(3):
            settlement.run_worker_once(c, RAIL, batch=10)
        c.close()

    ts = [threading.Thread(target=work) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    statuses = Counter(r["status"] for r in conn.execute("SELECT status FROM payments").fetchall())
    assert statuses == {"settled": 40}
    assert len(rail_rows(conn)) == 40
    assert settlement.reconcile(conn, RAIL)["ok"] and wallet.verify_ledger(conn)["ok"]


# ---------------------------------------------------------------- pending + holds


@pytest.fixture
def ask_key(conn):
    u = wallet.create_user(conn, "careful")
    wallet.create_merchant(conn, "shop", "Shop")
    wallet.fund(conn, u["user_id"], 10_000)
    k = wallet.issue_key(conn, u["user_id"], label="ask", per_payment_cap_cents=10_000,
                         daily_cap_cents=100_000, merchants=["shop"], confirm_above_cents=0)
    return {"conn": conn, "key": k.secret, "wallet": u["wallet_id"]}


def test_pending_payment_reserves_the_money(ask_key):
    c, k = ask_key["conn"], ask_key["key"]
    first = wallet.pay(c, k, "shop", 6_000, "a", rail=RAIL)
    assert first["status"] == "pending_confirmation"
    with pytest.raises(wallet.WalletError, match="INSUFFICIENT_FUNDS"):
        wallet.pay(c, k, "shop", 6_000, "b", rail=RAIL)
    assert wallet.confirm(c, first["payment_id"], rail=RAIL)["status"] == "settled"
    assert wallet.balance(c, ask_key["wallet"]) == 4_000


def test_decline_and_worker_expiry_release_the_hold(ask_key):
    c, k = ask_key["conn"], ask_key["key"]
    a = wallet.pay(c, k, "shop", 3_000, "a", rail=RAIL)
    b = wallet.pay(c, k, "shop", 3_000, "b", rail=RAIL)
    wallet.decline(c, a["payment_id"])
    from .conftest import backdate

    backdate(c, b["payment_id"], minutes=16)
    assert settlement.run_worker_once(c, RAIL)["expired"] == 1
    assert wallet.balance(c, ask_key["wallet"]) == 10_000
    assert wallet.verify_ledger(c)["ok"]


# ---------------------------------------------------------------- reconciliation


def test_reconcile_heals_processing_and_flags_real_drift(world):
    c, k = world["conn"], world["key"]
    lost = wallet.pay(c, k, "coffee", 1_000, "lost", rail=faulty("timeout_after"))  # rail paid, we don't know
    paid = wallet.pay(c, k, "coffee", 1_100, "paid", rail=RAIL)
    refused = wallet.pay(c, k, "books", 1_200, "refused", rail=faulty("decline"))
    assert lost["status"] == "processing"

    # Drift a real system could have: the provider later flips a decline to paid, loses a record,
    # and reports a transfer we never asked for.
    c.execute("UPDATE fake_rail.transfers SET status = 'succeeded' WHERE idempotency_key = %s", (refused["payment_id"],))
    c.execute("DELETE FROM fake_rail.transfers WHERE idempotency_key = %s", (paid["payment_id"],))
    c.execute("INSERT INTO fake_rail.transfers VALUES ('mystery', 'coffee', 999, 'succeeded', 'fr_x', '', now())")

    r = settlement.reconcile(c, RAIL)
    assert r["healed_processing"] == 1
    assert wallet.get_payment(c, k, lost["payment_id"])["status"] == "settled"
    assert sorted(i["kind"] for i in r["issues"]) == [
        "rail_paid_but_ledger_released", "settled_but_rail_did_not_pay", "unknown_rail_transfer"]
    assert c.execute("SELECT COUNT(*) AS n FROM recon_issues WHERE run_id = %s", (r["run_id"],)).fetchone()["n"] == 3


def test_reconcile_flags_stuck_payments(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "coffee", 1_000, "a")
    c.execute("UPDATE payments SET attempts = %s WHERE id = %s", (settlement.MAX_ATTEMPTS, p["payment_id"]))
    assert settlement.run_worker_once(c, RAIL)["claimed"] == 0  # given up on
    assert [i["kind"] for i in settlement.reconcile(c, RAIL)["issues"]] == ["stuck"]


# ---------------------------------------------------------------- chaos


def test_chaos_every_payment_ends_known_and_rail_matches_ledger(conn):
    """300 payments over 3 users, the rail misbehaving on ~40% of calls (declines, requests lost on
    the way, responses lost on the way back). Run workers until nothing is in flight, then demand
    an exact match between the rail's records and the ledger."""
    rng = random.Random(20261123)
    modes = ["ok"] * 60 + ["decline"] * 10 + ["timeout_before"] * 15 + ["timeout_after"] * 15
    injected = Counter()

    def fault(_key):
        m = rng.choice(modes)
        injected[m] += 1
        return m

    rail = FakeRail(TEST_DSN, faults=fault)
    for m in ("shop", "cafe", "books"):
        wallet.create_merchant(conn, m, m)
    keys = []
    for n in range(3):
        u = wallet.create_user(conn, f"user{n}")
        wallet.fund(conn, u["user_id"], 1_000_000)
        keys.append(wallet.issue_key(conn, u["user_id"], label="chaos", per_payment_cap_cents=5_000,
                                     daily_cap_cents=1_000_000, merchants=["shop", "cafe", "books"]).secret)

    first = Counter()
    for i in range(300):
        p = wallet.pay(conn, rng.choice(keys), rng.choice(["shop", "cafe", "books"]), rng.randint(1, 5_000),
                       f"c{i}", rail=rail)
        first[p["status"]] += 1
    assert injected["timeout_after"] > 0, "the chaos didn't produce any lost responses"

    for _ in range(settlement.MAX_ATTEMPTS):
        make_due(conn)
        if settlement.run_worker_once(conn, rail, batch=500)["claimed"] == 0:
            break

    final = Counter(r["status"] for r in conn.execute("SELECT status FROM payments").fetchall())
    assert set(final) <= {"settled", "failed"} and sum(final.values()) == 300

    ledger = {str(r["id"]): r["amount_cents"] for r in
              conn.execute("SELECT id, amount_cents FROM payments WHERE status = 'settled'").fetchall()}
    rail_paid = {r["idempotency_key"]: r["amount_cents"] for r in rail_rows(conn) if r["status"] == "succeeded"}
    out_twice = [k for k in rail_paid if k not in ledger]
    assert not out_twice, (f"{len(out_twice)} payments: rail paid the merchant but the wallet gave the money back "
                           f"(${sum(rail_paid[k] for k in out_twice) / 100:,.2f})")
    assert ledger == rail_paid  # same payments, same amounts, no extra, none missing
    assert len(rail_rows(conn)) == len({r["idempotency_key"] for r in rail_rows(conn)})
    merchants_got = conn.execute(
        "SELECT COALESCE(SUM(p.amount_cents), 0)::bigint AS s FROM postings p JOIN accounts a ON a.id = p.account_id"
        " WHERE a.kind = 'merchant'"
    ).fetchone()["s"]
    assert merchants_got == sum(rail_paid.values())
    held_total = conn.execute(
        "SELECT COALESCE(SUM(p.amount_cents), 0)::bigint AS s FROM postings p JOIN accounts a ON a.id = p.account_id"
        " WHERE a.kind = 'hold'"
    ).fetchone()["s"]
    assert held_total == 0
    assert wallet.verify_ledger(conn)["ok"]
    assert settlement.reconcile(conn, rail)["ok"]
    print(f"\nchaos: injected {dict(injected)}; first response {dict(first)}; final {dict(final)};"
          f" rail transfers {len(rail_rows(conn))}")
