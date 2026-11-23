"""The money-moving core. Every rule the agent must obey is checked here, inside one database
transaction, with the key row locked. Nothing in this file reads the memo or any other text the
agent wrote to make a decision: the prompt can say anything, the numbers decide.

Two kinds of callers:
- the agent, through the MCP server, holding only an API key  -> pay(), get_payment(), wallet_view()
- the user / merchant / operator, through the admin CLI       -> everything else
The MCP server never imports the admin functions.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg

KEY_PREFIX = "awk_"
MAX_AMOUNT_CENTS = 100_000_000  # $1M: anything above is a bug, not a payment
MAX_MEMO = 200
PENDING_TTL = timedelta(minutes=15)
DAY = timedelta(hours=24)


class WalletError(Exception):
    """A request the wallet refuses. `code` is stable and machine-readable; the message is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- helpers


def parse_amount(text: str) -> int:
    """'12.34' -> 1234. Rejects more than two decimals, negatives, zero, and garbage."""
    try:
        d = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise WalletError("BAD_AMOUNT", f"not an amount: {text!r}") from None
    if not d.is_finite() or d != d.quantize(Decimal("0.01")):
        raise WalletError("BAD_AMOUNT", f"amounts have at most two decimals: {text!r}")
    cents = int(d * 100)
    _check_amount(cents)
    return cents


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def _check_amount(cents: Any) -> int:
    # bool is a subclass of int: True would otherwise be a one-cent payment.
    if type(cents) is not int:
        raise WalletError("BAD_AMOUNT", "amount_cents must be an integer number of cents")
    if not 0 < cents <= MAX_AMOUNT_CENTS:
        raise WalletError("BAD_AMOUNT", f"amount_cents must be between 1 and {MAX_AMOUNT_CENTS}")
    return cents


def _check_idempotency_key(k: Any) -> str:
    if not isinstance(k, str) or not 1 <= len(k) <= 128 or not k.isprintable():
        raise WalletError("BAD_REQUEST", "idempotency_key must be 1-128 printable characters")
    return k


def hash_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def _request_hash(merchant_id: str, amount_cents: int, memo: str) -> str:
    return hashlib.sha256(f"{merchant_id}\x1f{amount_cents}\x1f{memo}".encode()).hexdigest()


def balance(conn: psycopg.Connection, account_id: UUID) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS b FROM postings WHERE account_id = %s", (account_id,)
    ).fetchone()
    return row["b"]


def _post(conn: psycopg.Connection, kind: str, ref_id: UUID | None, legs: list[tuple[UUID, int]]) -> UUID:
    """Write one balanced journal entry. The DB re-checks the balance at commit."""
    if sum(a for _, a in legs) != 0:
        raise AssertionError(f"unbalanced {kind} entry: {legs}")
    entry = conn.execute(
        "INSERT INTO journal_entries (kind, ref_id) VALUES (%s, %s) RETURNING id", (kind, ref_id)
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO postings (entry_id, account_id, amount_cents) VALUES (%s, %s, %s)",
            [(entry, acct, amt) for acct, amt in legs],
        )
    return entry


def _log(conn: psycopg.Connection, key_id, action, merchant_id, amount, outcome, reason="") -> None:
    conn.execute(
        "INSERT INTO decisions (key_id, action, merchant_id, amount_cents, outcome, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (key_id, action, merchant_id, amount if type(amount) is int else None, outcome, reason),
    )


def _payment_view(p: dict, replayed: bool = False) -> dict:
    return {
        "payment_id": str(p["id"]),
        "merchant_id": p["merchant_id"],
        "amount_cents": p["amount_cents"],
        "amount": fmt(p["amount_cents"]),
        "refunded_cents": p["refunded_cents"],
        "status": p["status"],
        "failure_reason": p["last_error"] if p["status"] == "failed" else "",
        "memo": p["memo"],
        "created_at": p["created_at"].isoformat(),
        "replayed": replayed,
    }


# ---------------------------------------------------------------- agent side


def _load_key(conn: psycopg.Connection, secret: Any, *, lock: bool) -> dict:
    if not isinstance(secret, str) or not secret.startswith(KEY_PREFIX):
        raise WalletError("KEY_INVALID", "unknown API key")
    row = conn.execute(
        "SELECT * FROM agent_keys WHERE key_hash = %s" + (" FOR UPDATE" if lock else ""), (hash_key(secret),)
    ).fetchone()
    if row is None:
        raise WalletError("KEY_INVALID", "unknown API key")
    if row["revoked_at"] is not None:
        raise WalletError("KEY_REVOKED", "this API key was revoked")
    if row["expires_at"] is not None and row["expires_at"] <= _now(conn):
        raise WalletError("KEY_EXPIRED", "this API key has expired")
    return row


def _now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now() AS t").fetchone()["t"]


def _spent_in_window(conn: psycopg.Connection, key_id: UUID) -> int:
    """What this key has used of its rolling 24h budget: settled and in-flight payments (refunds do
    NOT give the budget back: a refund is the merchant's act, not the agent's) plus live pending
    ones, which reserve their amount so a burst of pending payments can't overrun the cap. Failed,
    declined and expired payments moved no money and use no budget."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0)::bigint AS s FROM payments
           WHERE key_id = %s AND created_at > now() - %s
             AND (status IN ('settled', 'processing')
                  OR (status = 'pending_confirmation' AND created_at > now() - %s))""",
        (key_id, DAY, PENDING_TTL),
    ).fetchone()
    return row["s"]


def _allowed_merchants(conn: psycopg.Connection, key_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT m.id, m.name FROM key_merchants km JOIN merchants m ON m.id = km.merchant_id"
        " WHERE km.key_id = %s ORDER BY m.id",
        (key_id,),
    ).fetchall()


def pay(
    conn: psycopg.Connection,
    api_key: str,
    merchant_id: Any,
    amount_cents: Any,
    idempotency_key: Any,
    memo: Any = "",
    *,
    rail: Any = None,
) -> dict:
    """Pay a merchant from the key's wallet, in two steps:
    1. authorize (one DB transaction): every limit check, then move the money into the user's hold
       account. Commits before anything leaves the building.
    2. send to the rail (after commit, so a slow or dead rail never holds a DB lock), then capture
       to the merchant or release back to the wallet. If the outcome is unknown, the payment stays
       'processing' and the worker finishes it. With rail=None only step 1 runs."""
    view = _authorize(conn, api_key, merchant_id, amount_cents, idempotency_key, memo)
    if view["status"] == "processing" and not view["replayed"] and rail is not None:
        from . import settlement

        return settlement.attempt(conn, rail, UUID(view["payment_id"]))
    return view


def _hold_account(conn: psycopg.Connection, user_id: UUID) -> UUID:
    return conn.execute("SELECT id FROM accounts WHERE kind = 'hold' AND user_id = %s", (user_id,)).fetchone()["id"]


def _release(conn: psycopg.Connection, p: dict, status: str, reason: str = "") -> None:
    """Give held money back to the wallet and close the payment. Caller holds the payment row lock."""
    hold = _hold_account(conn, conn.execute("SELECT user_id FROM accounts WHERE id = %s", (p["wallet_id"],)).fetchone()["user_id"])
    _post(conn, "release", p["id"], [(hold, -p["amount_cents"]), (p["wallet_id"], p["amount_cents"])])
    conn.execute(
        "UPDATE payments SET status = %s, last_error = %s, next_attempt_at = NULL WHERE id = %s",
        (status, reason, p["id"]),
    )
    p["status"], p["last_error"] = status, reason


def _authorize(conn, api_key, merchant_id, amount_cents, idempotency_key, memo) -> dict:
    """Order of checks: key -> idempotent replay -> merchant allowlist -> per-payment cap -> rolling
    daily cap -> balance -> (maybe) ask the user. The key row is locked for the whole transaction,
    so two concurrent payments on one key run one after the other and both see each other's spend."""
    key_id = None
    try:
        _check_amount(amount_cents)
        _check_idempotency_key(idempotency_key)
        if not isinstance(merchant_id, str) or not merchant_id:
            raise WalletError("BAD_REQUEST", "merchant_id is required")
        if not isinstance(memo, str) or len(memo) > MAX_MEMO:
            raise WalletError("BAD_REQUEST", f"memo must be text of at most {MAX_MEMO} characters")

        with conn.transaction():
            key = _load_key(conn, api_key, lock=True)
            key_id = key["id"]
            if key["scope"] != "pay":
                raise WalletError("SCOPE", "this API key is read-only")

            rh = _request_hash(merchant_id, amount_cents, memo)
            prior = conn.execute(
                "SELECT * FROM payments WHERE key_id = %s AND idempotency_key = %s", (key_id, idempotency_key)
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != rh:
                    raise WalletError(
                        "IDEMPOTENCY_CONFLICT",
                        "this idempotency_key was already used for a different payment; use a new key",
                    )
                _log(conn, key_id, "pay", merchant_id, amount_cents, "replayed")
                return _payment_view(prior, replayed=True)

            # Same answer whether the merchant doesn't exist or just isn't allowed: don't help
            # an attacker map out the merchant list.
            allowed = conn.execute(
                "SELECT m.account_id FROM key_merchants km JOIN merchants m ON m.id = km.merchant_id"
                " WHERE km.key_id = %s AND km.merchant_id = %s",
                (key_id, merchant_id),
            ).fetchone()
            if allowed is None:
                raise WalletError("MERCHANT_NOT_ALLOWED", f"merchant {merchant_id!r} is not on this key's allowlist")

            if amount_cents > key["per_payment_cap_cents"]:
                raise WalletError(
                    "OVER_PER_PAYMENT_CAP",
                    f"{fmt(amount_cents)} is above the per-payment cap of {fmt(key['per_payment_cap_cents'])}",
                )
            spent = _spent_in_window(conn, key_id)
            if spent + amount_cents > key["daily_cap_cents"]:
                raise WalletError(
                    "OVER_DAILY_CAP",
                    f"{fmt(amount_cents)} would exceed the 24h cap of {fmt(key['daily_cap_cents'])}"
                    f" ({fmt(spent)} already used)",
                )

            conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (key["wallet_id"],))
            if balance(conn, key["wallet_id"]) < amount_cents:
                raise WalletError("INSUFFICIENT_FUNDS", "the wallet balance is too low for this payment")

            # Funds are held either way: a pending payment the user approves later can't bounce
            # because the agent spent the money elsewhere in the meantime.
            needs_user = key["confirm_above_cents"] is not None and amount_cents > key["confirm_above_cents"]
            status = "pending_confirmation" if needs_user else "processing"
            p = conn.execute(
                """INSERT INTO payments (key_id, wallet_id, merchant_id, amount_cents, memo, status,
                                         idempotency_key, request_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (key_id, key["wallet_id"], merchant_id, amount_cents, memo, status, idempotency_key, rh),
            ).fetchone()
            hold = _hold_account(conn, key["user_id"])
            _post(conn, "hold", p["id"], [(key["wallet_id"], -amount_cents), (hold, amount_cents)])
            _log(conn, key_id, "pay", merchant_id, amount_cents, status)
            return _payment_view(p)
    except WalletError as e:
        # The transaction above rolled back; record the refusal on its own.
        _log(conn, key_id, "pay", merchant_id if isinstance(merchant_id, str) else None, amount_cents, "denied", e.code)
        raise


def get_payment(conn: psycopg.Connection, api_key: str, payment_id: Any) -> dict:
    """A key can only see payments it made."""
    with conn.transaction():
        key = _load_key(conn, api_key, lock=False)
        try:
            pid = UUID(str(payment_id))
        except ValueError:
            raise WalletError("NOT_FOUND", "no such payment") from None
        p = conn.execute("SELECT * FROM payments WHERE id = %s AND key_id = %s", (pid, key["id"])).fetchone()
        if p is None:
            raise WalletError("NOT_FOUND", "no such payment")
        _expire_if_stale(conn, p)
        return _payment_view(p)


def list_payments(conn: psycopg.Connection, api_key: str, limit: int = 20) -> list[dict]:
    with conn.transaction():
        key = _load_key(conn, api_key, lock=False)
        rows = conn.execute(
            "SELECT * FROM payments WHERE key_id = %s ORDER BY created_at DESC LIMIT %s",
            (key["id"], max(1, min(int(limit), 100))),
        ).fetchall()
        return [_payment_view(p) for p in rows]


def wallet_view(conn: psycopg.Connection, api_key: str) -> dict:
    """What the agent may know: balance, its own limits, how much budget is left."""
    with conn.transaction():
        key = _load_key(conn, api_key, lock=False)
        spent = _spent_in_window(conn, key["id"])
        bal = balance(conn, key["wallet_id"])
        held = balance(conn, _hold_account(conn, key["user_id"]))
        return {
            "balance_cents": bal,
            "balance": fmt(bal),
            "held_cents": held,
            "scope": key["scope"],
            "per_payment_cap_cents": key["per_payment_cap_cents"],
            "daily_cap_cents": key["daily_cap_cents"],
            "used_last_24h_cents": spent,
            "remaining_last_24h_cents": max(0, key["daily_cap_cents"] - spent),
            "confirm_above_cents": key["confirm_above_cents"],
            "allowed_merchants": [dict(m) for m in _allowed_merchants(conn, key["id"])],
            "expires_at": key["expires_at"].isoformat() if key["expires_at"] else None,
        }


def _expire_if_stale(conn: psycopg.Connection, p: dict) -> None:
    if p["status"] == "pending_confirmation" and p["created_at"] <= _now(conn) - PENDING_TTL:
        locked = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (p["id"],)).fetchone()
        if locked["status"] == "pending_confirmation":
            _release(conn, locked, "expired", "user did not approve in time")
        p.update(locked)


# ---------------------------------------------------------------- user / operator side (CLI only)


@dataclass
class IssuedKey:
    key_id: UUID
    secret: str  # shown once, never stored


def create_user(conn: psycopg.Connection, name: str) -> dict:
    with conn.transaction():
        u = conn.execute("INSERT INTO users (name) VALUES (%s) RETURNING *", (name,)).fetchone()
        w = conn.execute(
            "INSERT INTO accounts (kind, user_id, name) VALUES ('wallet', %s, %s) RETURNING id",
            (u["id"], f"wallet:{name}"),
        ).fetchone()
        conn.execute("INSERT INTO accounts (kind, user_id, name) VALUES ('hold', %s, %s)", (u["id"], f"hold:{name}"))
        return {"user_id": u["id"], "wallet_id": w["id"], "name": name}


def create_merchant(conn: psycopg.Connection, merchant_id: str, name: str) -> dict:
    with conn.transaction():
        a = conn.execute(
            "INSERT INTO accounts (kind, name) VALUES ('merchant', %s) RETURNING id", (f"merchant:{merchant_id}",)
        ).fetchone()
        conn.execute("INSERT INTO merchants (id, name, account_id) VALUES (%s, %s, %s)", (merchant_id, name, a["id"]))
        return {"merchant_id": merchant_id, "account_id": a["id"]}


def _funding_account(conn: psycopg.Connection) -> UUID:
    row = conn.execute("SELECT id FROM accounts WHERE kind = 'funding'").fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO accounts (kind, name) VALUES ('funding', 'external:top-ups')"
        " ON CONFLICT (kind) WHERE kind = 'funding' DO UPDATE SET name = EXCLUDED.name RETURNING id"
    ).fetchone()["id"]


def wallet_of(conn: psycopg.Connection, user_id: UUID) -> UUID:
    row = conn.execute("SELECT id FROM accounts WHERE kind = 'wallet' AND user_id = %s", (user_id,)).fetchone()
    if row is None:
        raise WalletError("NOT_FOUND", "no such user")
    return row["id"]


def fund(conn: psycopg.Connection, user_id: UUID, amount_cents: int) -> int:
    """Top up a wallet (in real life: a bank transfer or card load landed). Returns the new balance."""
    _check_amount(amount_cents)
    with conn.transaction():
        w = wallet_of(conn, user_id)
        _post(conn, "fund", None, [(_funding_account(conn), -amount_cents), (w, amount_cents)])
        return balance(conn, w)


def issue_key(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    label: str,
    scope: str = "pay",
    per_payment_cap_cents: int,
    daily_cap_cents: int,
    merchants: list[str],
    confirm_above_cents: int | None = None,
    expires_in: timedelta | None = None,
) -> IssuedKey:
    if scope not in ("read", "pay"):
        raise WalletError("BAD_REQUEST", "scope must be read or pay")
    if per_payment_cap_cents > daily_cap_cents:
        raise WalletError("BAD_REQUEST", "per-payment cap can't be above the daily cap")
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    with conn.transaction():
        w = wallet_of(conn, user_id)
        k = conn.execute(
            """INSERT INTO agent_keys (user_id, wallet_id, label, key_prefix, key_hash, scope,
                   per_payment_cap_cents, daily_cap_cents, confirm_above_cents, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                user_id, w, label, secret[:10], hash_key(secret), scope, per_payment_cap_cents, daily_cap_cents,
                confirm_above_cents, (datetime.now(timezone.utc) + expires_in) if expires_in else None,
            ),
        ).fetchone()
        for m in merchants:
            if conn.execute("SELECT 1 FROM merchants WHERE id = %s", (m,)).fetchone() is None:
                raise WalletError("NOT_FOUND", f"no merchant {m!r}")
            conn.execute("INSERT INTO key_merchants (key_id, merchant_id) VALUES (%s, %s)", (k["id"], m))
        return IssuedKey(k["id"], secret)


def revoke_key(conn: psycopg.Connection, key_id: UUID) -> None:
    with conn.transaction():
        r = conn.execute(
            "UPDATE agent_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,)
        )
        if r.rowcount == 0:
            raise WalletError("NOT_FOUND", "no active key with that id")
        # Anything still waiting for the user dies with the key. Payments already sent to the rail
        # ('processing') were authorized while the key was valid and are left to finish.
        for p in conn.execute(
            "SELECT * FROM payments WHERE key_id = %s AND status = 'pending_confirmation' FOR UPDATE", (key_id,)
        ).fetchall():
            _release(conn, p, "declined", "key revoked")


def confirm(conn: psycopg.Connection, payment_id: UUID, *, rail: Any = None) -> dict:
    """The user approves a payment the agent asked for. Runs outside the agent's reach (CLI / app).
    The money is already held, so approval can't bounce; it just releases the payment to the rail."""
    with conn.transaction():
        head = conn.execute("SELECT key_id FROM payments WHERE id = %s", (payment_id,)).fetchone()
        if head is None:
            raise WalletError("NOT_FOUND", "no such payment")
        key = conn.execute("SELECT * FROM agent_keys WHERE id = %s FOR UPDATE", (head["key_id"],)).fetchone()
        p = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()
        if p["status"] != "pending_confirmation":
            raise WalletError("BAD_STATE", f"payment is {p['status']}, not pending")
        if p["created_at"] <= _now(conn) - PENDING_TTL:
            _release(conn, p, "expired", "user did not approve in time")
            return {"payment_id": str(payment_id), "status": "expired"}
        if key["revoked_at"] is not None:
            _release(conn, p, "declined", "key revoked")
            return {"payment_id": str(payment_id), "status": "declined"}
        conn.execute("UPDATE payments SET status = 'processing' WHERE id = %s", (payment_id,))
    if rail is not None:
        from . import settlement

        return settlement.attempt(conn, rail, payment_id)
    return {"payment_id": str(payment_id), "status": "processing"}


def decline(conn: psycopg.Connection, payment_id: UUID) -> dict:
    with conn.transaction():
        p = conn.execute(
            "SELECT * FROM payments WHERE id = %s AND status = 'pending_confirmation' FOR UPDATE", (payment_id,)
        ).fetchone()
        if p is None:
            raise WalletError("BAD_STATE", "no pending payment with that id")
        _release(conn, p, "declined", "declined by user")
        return {"payment_id": str(payment_id), "status": "declined"}


def refund(
    conn: psycopg.Connection, payment_id: UUID, amount_cents: int, idempotency_key: str, reason: str = ""
) -> dict:
    """Merchant-initiated refund, full or partial. Never exposed to the agent: an agent that could
    refund could also move money between merchants and the wallet at will."""
    _check_amount(amount_cents)
    _check_idempotency_key(idempotency_key)
    with conn.transaction():
        p = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()
        if p is None:
            raise WalletError("NOT_FOUND", "no such payment")
        prior = conn.execute(
            "SELECT * FROM refunds WHERE payment_id = %s AND idempotency_key = %s", (payment_id, idempotency_key)
        ).fetchone()
        if prior is not None:
            if prior["amount_cents"] != amount_cents:
                raise WalletError("IDEMPOTENCY_CONFLICT", "this idempotency_key was used for a different refund")
            return {"refund_id": str(prior["id"]), "payment_id": str(payment_id), "amount_cents": amount_cents,
                    "refunded_cents": p["refunded_cents"], "replayed": True}
        if p["status"] != "settled":
            raise WalletError("BAD_STATE", f"payment is {p['status']}; only settled payments can be refunded")
        left = p["amount_cents"] - p["refunded_cents"]
        if amount_cents > left:
            raise WalletError("REFUND_TOO_LARGE", f"only {fmt(left)} of this payment is left to refund")
        r = conn.execute(
            "INSERT INTO refunds (payment_id, amount_cents, reason, idempotency_key) VALUES (%s, %s, %s, %s) RETURNING id",
            (payment_id, amount_cents, reason, idempotency_key),
        ).fetchone()
        m = conn.execute("SELECT account_id FROM merchants WHERE id = %s", (p["merchant_id"],)).fetchone()
        _post(conn, "refund", r["id"], [(m["account_id"], -amount_cents), (p["wallet_id"], amount_cents)])
        conn.execute("UPDATE payments SET refunded_cents = refunded_cents + %s WHERE id = %s", (amount_cents, payment_id))
        return {"refund_id": str(r["id"]), "payment_id": str(payment_id), "amount_cents": amount_cents,
                "refunded_cents": p["refunded_cents"] + amount_cents, "replayed": False}


def verify_ledger(conn: psycopg.Connection) -> dict:
    """Audit everything from scratch. Each list should be empty; `ok` says whether they all are."""
    with conn.transaction():
        unbalanced = conn.execute(
            "SELECT entry_id FROM postings GROUP BY entry_id HAVING SUM(amount_cents) <> 0"
        ).fetchall()
        negative_wallets = conn.execute(
            """SELECT a.id FROM accounts a JOIN postings p ON p.account_id = a.id
               WHERE a.kind IN ('wallet', 'hold') GROUP BY a.id HAVING SUM(p.amount_cents) < 0"""
        ).fetchall()
        total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0)::bigint AS t FROM postings").fetchone()["t"]
        # Payments table and ledger must tell the same story, payment by payment:
        #   still held   = amount while pending/processing, else 0
        #   paid out     = amount if settled, else 0
        #   refunded     = refunds table = payments.refunded_cents
        mismatched = conn.execute(
            """WITH moves AS (
                   SELECT je.ref_id,
                          SUM(po.amount_cents) FILTER (WHERE a.kind = 'hold') AS held,
                          SUM(po.amount_cents) FILTER (WHERE je.kind = 'capture' AND a.kind = 'merchant') AS paid
                   FROM journal_entries je
                   JOIN postings po ON po.entry_id = je.id
                   JOIN accounts a ON a.id = po.account_id
                   WHERE je.kind IN ('hold', 'capture', 'release') GROUP BY je.ref_id)
               SELECT p.id FROM payments p
               LEFT JOIN moves m ON m.ref_id = p.id
               LEFT JOIN (SELECT payment_id, SUM(amount_cents) AS r FROM refunds GROUP BY payment_id) rf
                      ON rf.payment_id = p.id
               WHERE COALESCE(m.held, 0) <> CASE WHEN p.status IN ('pending_confirmation', 'processing')
                                                 THEN p.amount_cents ELSE 0 END
                  OR COALESCE(m.paid, 0) <> CASE WHEN p.status = 'settled' THEN p.amount_cents ELSE 0 END
                  OR COALESCE(rf.r, 0) <> p.refunded_cents"""
        ).fetchall()
        over_cap = conn.execute(
            # No key ever had more money out (settled or in flight) in any 24h window than its cap.
            """SELECT DISTINCT a.key_id FROM payments a
               JOIN agent_keys k ON k.id = a.key_id
               WHERE a.status IN ('settled', 'processing')
                 AND (SELECT SUM(b.amount_cents) FROM payments b
                      WHERE b.key_id = a.key_id AND b.status IN ('settled', 'processing')
                        AND b.created_at > a.created_at - interval '24 hours' AND b.created_at <= a.created_at)
                     > k.daily_cap_cents"""
        ).fetchall()
        out = {
            "unbalanced_entries": [str(r["entry_id"]) for r in unbalanced],
            "negative_wallets": [str(r["id"]) for r in negative_wallets],
            "ledger_sum_cents": total,
            "payments_not_matching_ledger": [str(r["id"]) for r in mismatched],
            "keys_over_daily_cap": [str(r["key_id"]) for r in over_cap],
        }
        out["ok"] = total == 0 and not any(v for k, v in out.items() if isinstance(v, list))
        return out
