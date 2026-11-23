"""Payments, limits, idempotency, user confirmation and refunds: the normal rules."""

from __future__ import annotations

import pytest

from agent_wallet import wallet
from agent_wallet.wallet import WalletError

from .conftest import backdate


def bal(w):
    return wallet.balance(w["conn"], w["user"]["wallet_id"])


def codes(fn, *a, **kw) -> str:
    with pytest.raises(WalletError) as e:
        fn(*a, **kw)
    return e.value.code


def test_pay_moves_money_and_balances(world):
    p = wallet.pay(world["conn"], world["key"], "coffee", 1_250, "order-1", "latte")
    assert p["status"] == "settled" and p["amount"] == "$12.50"
    assert bal(world) == 50_000 - 1_250
    assert wallet.verify_ledger(world["conn"])["ok"]


@pytest.mark.parametrize(
    "merchant, amount, code",
    [
        ("attacker", 100, "MERCHANT_NOT_ALLOWED"),
        ("no-such-merchant", 100, "MERCHANT_NOT_ALLOWED"),  # same answer: no merchant enumeration
        ("coffee", 5_001, "OVER_PER_PAYMENT_CAP"),
        ("coffee", 0, "BAD_AMOUNT"),
        ("coffee", -500, "BAD_AMOUNT"),
        ("coffee", 12.5, "BAD_AMOUNT"),
        ("coffee", "1250", "BAD_AMOUNT"),
        ("coffee", True, "BAD_AMOUNT"),
    ],
)
def test_single_payment_rules(world, merchant, amount, code):
    assert codes(wallet.pay, world["conn"], world["key"], merchant, amount, "k") == code
    assert bal(world) == 50_000


def test_daily_cap_is_rolling_24h(world):
    c, k = world["conn"], world["key"]
    a = wallet.pay(c, k, "coffee", 5_000, "a")
    wallet.pay(c, k, "books", 4_000, "b")
    assert codes(wallet.pay, c, k, "coffee", 1_001, "c") == "OVER_DAILY_CAP"
    wallet.pay(c, k, "coffee", 1_000, "d")  # exactly at the cap is fine
    assert codes(wallet.pay, c, k, "coffee", 1, "e") == "OVER_DAILY_CAP"
    backdate(c, a["payment_id"], hours=24, seconds=1)  # the first $50 falls out of the window
    wallet.pay(c, k, "coffee", 5_000, "f")
    assert wallet.wallet_view(c, k)["remaining_last_24h_cents"] == 0


def test_insufficient_funds(conn):
    u = wallet.create_user(conn, "poor")
    wallet.create_merchant(conn, "coffee", "Coffee")
    wallet.fund(conn, u["user_id"], 300)
    k = wallet.issue_key(conn, u["user_id"], label="a", per_payment_cap_cents=1_000, daily_cap_cents=1_000,
                         merchants=["coffee"])
    assert codes(wallet.pay, conn, k.secret, "coffee", 301, "x") == "INSUFFICIENT_FUNDS"
    wallet.pay(conn, k.secret, "coffee", 300, "y")
    assert wallet.balance(conn, u["wallet_id"]) == 0


def test_retry_with_same_idempotency_key_charges_once(world):
    c, k = world["conn"], world["key"]
    first = wallet.pay(c, k, "coffee", 1_250, "order-1", "latte")
    again = wallet.pay(c, k, "coffee", 1_250, "order-1", "latte")
    assert again["payment_id"] == first["payment_id"] and again["replayed"] is True
    assert bal(world) == 50_000 - 1_250


def test_reusing_idempotency_key_for_a_different_payment_is_refused(world):
    c, k = world["conn"], world["key"]
    wallet.pay(c, k, "coffee", 1_250, "order-1")
    assert codes(wallet.pay, c, k, "coffee", 4_999, "order-1") == "IDEMPOTENCY_CONFLICT"
    assert codes(wallet.pay, c, k, "books", 1_250, "order-1") == "IDEMPOTENCY_CONFLICT"
    assert bal(world) == 50_000 - 1_250


def test_denied_payment_does_not_burn_the_idempotency_key(world):
    c, k = world["conn"], world["key"]
    assert codes(wallet.pay, c, k, "coffee", 9_999, "order-1") == "OVER_PER_PAYMENT_CAP"
    assert wallet.pay(c, k, "coffee", 999, "order-1")["status"] == "settled"


def test_denials_are_logged_with_reason(world):
    c, k = world["conn"], world["key"]
    for amt in (9_999, 9_998):
        codes(wallet.pay, c, k, "coffee", amt, "x")
    rows = c.execute("SELECT outcome, reason FROM decisions ORDER BY id").fetchall()
    assert [r["reason"] for r in rows] == ["OVER_PER_PAYMENT_CAP"] * 2


# ---------------------------------------------------------------- user confirmation


@pytest.fixture
def confirm_key(world):
    k = wallet.issue_key(world["conn"], world["user"]["user_id"], label="careful", per_payment_cap_cents=5_000,
                         daily_cap_cents=10_000, merchants=["coffee"], confirm_above_cents=2_000)
    return k


def test_small_payments_settle_large_ones_wait_for_the_user(world, confirm_key):
    c = world["conn"]
    assert wallet.pay(c, confirm_key.secret, "coffee", 2_000, "small")["status"] == "settled"
    p = wallet.pay(c, confirm_key.secret, "coffee", 4_000, "big")
    assert p["status"] == "pending_confirmation"
    assert bal(world) == 50_000 - 2_000  # nothing moved yet
    assert wallet.confirm(c, p["payment_id"])["status"] == "settled"
    assert bal(world) == 50_000 - 6_000
    assert wallet.get_payment(c, confirm_key.secret, p["payment_id"])["status"] == "settled"
    assert wallet.verify_ledger(c)["ok"]


def test_pending_payments_reserve_the_daily_budget(world, confirm_key):
    c, k = world["conn"], confirm_key.secret
    wallet.pay(c, k, "coffee", 5_000, "p1")
    wallet.pay(c, k, "coffee", 5_000, "p2")
    assert codes(wallet.pay, c, k, "coffee", 100, "p3") == "OVER_DAILY_CAP"


def test_declined_and_expired_pending(world, confirm_key):
    c, k = world["conn"], confirm_key.secret
    p1 = wallet.pay(c, k, "coffee", 4_000, "p1")
    wallet.decline(c, p1["payment_id"])
    assert codes(wallet.confirm, c, p1["payment_id"]) == "BAD_STATE"
    p2 = wallet.pay(c, k, "coffee", 4_000, "p2")
    backdate(c, p2["payment_id"], minutes=16)
    assert wallet.confirm(c, p2["payment_id"])["status"] == "expired"
    assert bal(world) == 50_000
    assert wallet.wallet_view(c, k)["used_last_24h_cents"] == 0  # neither one holds budget any more


def test_revoking_a_key_kills_its_pending_payments(world, confirm_key):
    c = world["conn"]
    p = wallet.pay(c, confirm_key.secret, "coffee", 4_000, "p")
    wallet.revoke_key(c, confirm_key.key_id)
    assert codes(wallet.confirm, c, p["payment_id"]) == "BAD_STATE"
    assert bal(world) == 50_000


# ---------------------------------------------------------------- refunds


def test_partial_then_full_refund(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "books", 4_000, "book")
    wallet.refund(c, p["payment_id"], 1_500, "r1", "one book missing")
    assert bal(world) == 50_000 - 2_500
    assert codes(wallet.refund, c, p["payment_id"], 2_501, "r2") == "REFUND_TOO_LARGE"
    wallet.refund(c, p["payment_id"], 2_500, "r2")
    assert bal(world) == 50_000
    assert codes(wallet.refund, c, p["payment_id"], 1, "r3") == "REFUND_TOO_LARGE"
    assert wallet.verify_ledger(c)["ok"]


def test_refund_is_idempotent(world):
    c = world["conn"]
    p = wallet.pay(c, world["key"], "books", 4_000, "book")
    r1 = wallet.refund(c, p["payment_id"], 1_000, "r1")
    r2 = wallet.refund(c, p["payment_id"], 1_000, "r1")
    assert r2["refund_id"] == r1["refund_id"] and r2["replayed"]
    assert bal(world) == 50_000 - 3_000
    assert codes(wallet.refund, c, p["payment_id"], 2_000, "r1") == "IDEMPOTENCY_CONFLICT"


def test_refund_does_not_give_the_agent_budget_back(world):
    """Otherwise an agent colluding with a merchant could buy, get refunded, and buy again forever."""
    c, k = world["conn"], world["key"]
    p = wallet.pay(c, k, "books", 5_000, "a")
    wallet.pay(c, k, "books", 5_000, "b")
    wallet.refund(c, p["payment_id"], 5_000, "r")
    assert codes(wallet.pay, c, k, "books", 100, "c") == "OVER_DAILY_CAP"


def test_cannot_refund_unsettled_payment(world, confirm_key):
    c = world["conn"]
    p = wallet.pay(c, confirm_key.secret, "coffee", 4_000, "p")
    assert codes(wallet.refund, c, p["payment_id"], 100, "r") == "BAD_STATE"


def test_parse_amount():
    assert wallet.parse_amount("12.34") == 1234
    assert wallet.parse_amount("50") == 5000
    for bad in ("12.345", "-1", "0", "abc", "NaN", "1e3.5"):
        with pytest.raises(WalletError):
            wallet.parse_amount(bad)
