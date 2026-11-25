"""Merchant-signed carts: the amount comes from the merchant's signature, never from the agent."""

from __future__ import annotations

import base64
import json
import threading
from datetime import timedelta

import pytest

from agent_wallet import carts, wallet
from agent_wallet.db import connect
from agent_wallet.wallet import WalletError

from .conftest import RAIL, TEST_DSN


@pytest.fixture
def shop(world):
    """coffee and books get signing keys; `attacker` gets its own (a real merchant can sign too)."""
    c = world["conn"]
    keys = {}
    for mid in ("coffee", "books", "attacker"):
        sk, pk = carts.new_merchant_key()
        c.execute("UPDATE merchants SET public_key = %s WHERE id = %s", (pk, mid))
        keys[mid] = sk
    return keys


def cart(sk, merchant="coffee", cart_id="c1", items=None, ttl=timedelta(minutes=15)):
    items = items or [{"name": "Large latte", "qty": 1, "unit_cents": 525}]
    return carts.sign_cart(sk, carts.make_cart(merchant, cart_id, items, ttl))


def code(fn, *a, **kw) -> str:
    with pytest.raises(WalletError) as e:
        fn(*a, **kw)
    return e.value.code


def test_signed_cart_pays_the_signed_total(world, shop):
    c = world["conn"]
    p = wallet.pay_cart(c, world["key"], cart(shop["coffee"], items=[
        {"name": "Large latte", "qty": 2, "unit_cents": 525}, {"name": "Croissant", "qty": 1, "unit_cents": 350}]),
        "order-1", rail=RAIL)
    assert (p["status"], p["merchant_id"], p["amount_cents"]) == ("settled", "coffee", 1400)
    assert [i["name"] for i in p["cart_items"]] == ["Large latte", "Croissant"]
    assert wallet.verify_ledger(c)["ok"]


def test_changing_the_price_breaks_the_signature(world, shop):
    """The injected-page attack: the agent (or the page) edits the total after the merchant signed."""
    token = cart(shop["coffee"])
    body, sig = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["items"][0]["unit_cents"] = payload["total_cents"] = 4725
    forged = base64.urlsafe_b64encode(carts.canonical(payload)).rstrip(b"=").decode() + "." + sig
    assert code(wallet.pay_cart, world["conn"], world["key"], forged, "o1") == "CART_SIGNATURE_INVALID"


def test_a_cart_signed_by_someone_else_is_refused(world, shop):
    """The attacker signs a $47.25 cart that claims to be from coffee, with the attacker's own key."""
    fake = cart(shop["attacker"], merchant="coffee", items=[{"name": "Large latte", "qty": 1, "unit_cents": 4725}])
    assert code(wallet.pay_cart, world["conn"], world["key"], fake, "o1") == "CART_SIGNATURE_INVALID"


def test_merchant_without_a_registered_key_cannot_take_carts(world):
    sk, _ = carts.new_merchant_key()
    assert code(wallet.pay_cart, world["conn"], world["key"], cart(sk), "o1") == "CART_SIGNATURE_INVALID"


def test_limits_and_allowlist_still_apply_to_signed_carts(world, shop):
    """A signature proves the merchant's price, not that the merchant is honest or allowed."""
    c, k = world["conn"], world["key"]
    assert code(wallet.pay_cart, c, k, cart(shop["attacker"], merchant="attacker"), "o1") == "MERCHANT_NOT_ALLOWED"
    big = cart(shop["books"], merchant="books", items=[{"name": "Box set", "qty": 2, "unit_cents": 3600}])
    assert code(wallet.pay_cart, c, k, big, "o2") == "OVER_PER_PAYMENT_CAP"


def test_each_cart_is_paid_once(world, shop):
    """The pay-it-three-times attack: same cart, fresh idempotency keys. A true retry still replays."""
    c, k = world["conn"], world["key"]
    token = cart(shop["coffee"])
    first = wallet.pay_cart(c, k, token, "latte-1", rail=RAIL)
    assert wallet.pay_cart(c, k, token, "latte-1", rail=RAIL)["replayed"]
    assert code(wallet.pay_cart, c, k, token, "latte-2") == "CART_ALREADY_PAID"
    assert code(wallet.pay_cart, c, k, token, "latte-3") == "CART_ALREADY_PAID"
    assert wallet.wallet_view(c, k)["balance_cents"] == 50_000 - first["amount_cents"]


def test_same_cart_from_two_keys_at_once_pays_once(world, shop):
    """Each key's row lock doesn't cover the other key; the unique index does."""
    c = world["conn"]
    other = wallet.create_user(c, "bob")
    wallet.fund(c, other["user_id"], 10_000)
    k2 = wallet.issue_key(c, other["user_id"], label="b", per_payment_cap_cents=5_000, daily_cap_cents=10_000,
                          merchants=["coffee"]).secret
    token, results, barrier = cart(shop["coffee"]), [], threading.Barrier(2)

    def go(key, idem):
        conn = connect(TEST_DSN)
        barrier.wait()
        try:
            results.append(wallet.pay_cart(conn, key, token, idem, rail=RAIL)["status"])
        except WalletError as e:
            results.append(e.code)
        finally:
            conn.close()

    ts = [threading.Thread(target=go, args=(world["key"], "a")), threading.Thread(target=go, args=(k2, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["CART_ALREADY_PAID", "settled"]
    assert wallet.verify_ledger(c)["ok"]


def test_a_declined_cart_can_be_paid_again(world, shop):
    c = world["conn"]
    k = wallet.issue_key(c, world["user"]["user_id"], label="ask", per_payment_cap_cents=5_000,
                         daily_cap_cents=10_000, merchants=["coffee"], confirm_above_cents=0).secret
    token = cart(shop["coffee"])
    p = wallet.pay_cart(c, k, token, "try-1", rail=RAIL)
    assert p["status"] == "pending_confirmation"
    wallet.decline(c, p["payment_id"])
    assert wallet.pay_cart(c, k, token, "try-2", rail=RAIL)["status"] == "pending_confirmation"


def test_expired_and_malformed_carts(world, shop):
    c, k = world["conn"], world["key"]
    assert code(wallet.pay_cart, c, k, cart(shop["coffee"], ttl=timedelta(seconds=-1)), "o1") == "CART_EXPIRED"
    for junk in ["", "abc", "a.b.c", 42, None, "x" * 9000]:
        assert code(wallet.pay_cart, c, k, junk, "o2") == "BAD_CART"
    with pytest.raises(carts.CartError):  # a merchant can't even sign a cart whose total doesn't add up
        bad = carts.make_cart("coffee", "c9", [{"name": "Latte", "qty": 1, "unit_cents": 525}])
        bad["total_cents"] = 1
        carts.sign_cart(shop["coffee"], bad)


def test_cart_by_reference(world, shop):
    """The checkout hands the signed cart to the wallet; the agent carries only 'coffee/c1'."""
    c, k = world["conn"], world["key"]
    ref = wallet.submit_cart(c, cart(shop["coffee"]))
    assert ref == "coffee/c1"
    assert wallet.pay_cart(c, k, ref, "o1", rail=RAIL)["amount_cents"] == 525
    assert code(wallet.pay_cart, c, k, "coffee/nope", "o2") == "CART_NOT_FOUND"
    forged = cart(shop["attacker"], merchant="coffee", cart_id="c2")
    assert code(wallet.submit_cart, c, forged) == "CART_SIGNATURE_INVALID"  # can't plant a cart either


def test_signed_carts_only_key_refuses_free_form_amounts(world, shop):
    c = world["conn"]
    k = wallet.issue_key(c, world["user"]["user_id"], label="strict", per_payment_cap_cents=5_000,
                         daily_cap_cents=10_000, merchants=["coffee"], require_signed_cart=True).secret
    assert code(wallet.pay, c, k, "coffee", 525, "o1") == "SIGNED_CART_REQUIRED"
    assert wallet.pay_cart(c, k, cart(shop["coffee"]), "o2", rail=RAIL)["status"] == "settled"
    assert wallet.wallet_view(c, k)["signed_carts_only"] is True
