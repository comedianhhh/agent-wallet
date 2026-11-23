"""Many payments at the same instant, each on its own connection, the way several agent processes
(or one agent firing parallel tool calls) would hit the wallet."""

from __future__ import annotations

import threading

from agent_wallet import wallet

from .conftest import RAIL
from agent_wallet.db import connect
from agent_wallet.wallet import WalletError


def burst(dsn, key, n, amount, merchant="coffee"):
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def one(i):
        c = connect(dsn)
        try:
            barrier.wait()
            try:
                out = wallet.pay(c, key, merchant, amount, f"burst-{i}", rail=RAIL)["status"]
            except WalletError as e:
                out = e.code
            with lock:
                results.append(out)
        finally:
            c.close()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_daily_cap_holds_under_a_burst(dsn, world):
    # $100/day cap, 20 simultaneous $30 payments: exactly 3 may go through.
    r = burst(dsn, world["key"], 20, 3_000)
    assert r.count("settled") == 3
    assert r.count("OVER_DAILY_CAP") == 17
    assert wallet.balance(world["conn"], world["user"]["wallet_id"]) == 50_000 - 9_000
    assert wallet.verify_ledger(world["conn"])["ok"]


def test_balance_holds_when_two_keys_share_a_wallet(dsn, conn):
    """Locks on the key alone would not be enough: two keys, one wallet, $50 in it."""
    u = wallet.create_user(conn, "shared")
    wallet.create_merchant(conn, "coffee", "Coffee")
    wallet.fund(conn, u["user_id"], 5_000)
    keys = [
        wallet.issue_key(conn, u["user_id"], label=f"agent-{i}", per_payment_cap_cents=2_000,
                         daily_cap_cents=100_000, merchants=["coffee"]).secret
        for i in range(2)
    ]
    out: list[list[str]] = []
    ts = [threading.Thread(target=lambda k=k: out.append(burst(dsn, k, 10, 2_000))) for k in keys]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    flat = [x for r in out for x in r]
    assert flat.count("settled") == 2  # 2 x $20 = $40; a third would need $60
    assert wallet.balance(conn, u["wallet_id"]) == 1_000
    assert wallet.verify_ledger(conn)["ok"]
