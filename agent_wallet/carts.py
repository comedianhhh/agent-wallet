"""Merchant-signed carts (the idea behind AP2's cart mandate and Stripe's scoped payment tokens).

The merchant's own checkout prices the cart and signs it. The wallet verifies the signature against
the merchant's registered public key and charges the signed total. The agent only carries the token:
it can't change the price, and text on a web page can't either, because nothing the agent writes is
part of what gets charged.

Token: base64url(canonical JSON cart) "." base64url(Ed25519 signature over those bytes).
Cart:  {"merchant_id", "cart_id", "items": [{"name", "qty", "unit_cents"}], "total_cents",
        "currency": "CAD", "expires_at": ISO-8601 UTC}
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_TOKEN = 8192
MAX_ITEMS = 50
CURRENCY = "CAD"


class CartError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(cart: dict) -> bytes:
    return json.dumps(cart, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def new_merchant_key() -> tuple[bytes, bytes]:
    """(private, public), both 32 raw bytes. The private key lives at the merchant, never in the wallet."""
    sk = Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return (sk.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
            sk.public_key().public_bytes(raw, serialization.PublicFormat.Raw))


def make_cart(merchant_id: str, cart_id: str, items: list[dict], ttl: timedelta = timedelta(minutes=15)) -> dict:
    return {
        "merchant_id": merchant_id, "cart_id": cart_id,
        "items": [{"name": i["name"], "qty": i["qty"], "unit_cents": i["unit_cents"]} for i in items],
        "total_cents": sum(i["qty"] * i["unit_cents"] for i in items),
        "currency": CURRENCY,
        "expires_at": (datetime.now(timezone.utc) + ttl).isoformat(timespec="seconds"),
    }


def sign_cart(private_key: bytes, cart: dict) -> str:
    """What the merchant's checkout does."""
    _check_shape(cart)
    body = canonical(cart)
    return f"{_b64e(body)}.{_b64e(Ed25519PrivateKey.from_private_bytes(private_key).sign(body))}"


def peek(token: object) -> dict:
    """Decode and shape-check WITHOUT verifying: only to learn which merchant's key to verify with."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN or token.count(".") != 1:
        raise CartError("BAD_CART", "cart must be the signed cart token from the merchant's checkout")
    try:
        cart = json.loads(_b64d(token.split(".")[0]))
    except (binascii.Error, ValueError):
        raise CartError("BAD_CART", "cart token is not readable") from None
    _check_shape(cart)
    return cart


def verify(token: str, public_key: bytes, now: datetime) -> dict:
    cart = peek(token)
    body_b64, sig_b64 = token.split(".")
    body = _b64d(body_b64)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(_b64d(sig_b64), body)
    except (InvalidSignature, binascii.Error, ValueError):
        raise CartError("CART_SIGNATURE_INVALID", "the cart was not signed by this merchant, or was changed") from None
    if body != canonical(cart):
        raise CartError("BAD_CART", "cart is not in canonical form")
    if datetime.fromisoformat(cart["expires_at"]) <= now:
        raise CartError("CART_EXPIRED", "the merchant's cart has expired; check out again")
    return cart


def _check_shape(cart: object) -> None:
    def bad(why: str):
        raise CartError("BAD_CART", why)

    if not isinstance(cart, dict) or set(cart) != {"merchant_id", "cart_id", "items", "total_cents", "currency",
                                                   "expires_at"}:
        bad("cart has the wrong fields")
    for f in ("merchant_id", "cart_id"):
        if not isinstance(cart[f], str) or not 1 <= len(cart[f]) <= 128:
            bad(f"{f} must be 1-128 characters")
    items = cart["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        bad(f"a cart has 1-{MAX_ITEMS} items")
    for i in items:
        if (not isinstance(i, dict) or set(i) != {"name", "qty", "unit_cents"} or not isinstance(i["name"], str)
                or not 1 <= len(i["name"]) <= 200 or type(i["qty"]) is not int or not 1 <= i["qty"] <= 1000
                or type(i["unit_cents"]) is not int or i["unit_cents"] < 1):
            bad("each item needs a name, a whole qty and a positive unit_cents")
    if type(cart["total_cents"]) is not int or cart["total_cents"] != sum(i["qty"] * i["unit_cents"] for i in items):
        bad("total_cents is not the sum of the items")
    if cart["currency"] != CURRENCY:
        bad(f"only {CURRENCY} carts are supported")
    try:
        exp = datetime.fromisoformat(cart["expires_at"])
    except (TypeError, ValueError):
        bad("expires_at must be an ISO-8601 time")
    if exp.tzinfo is None:
        bad("expires_at needs a time zone")
