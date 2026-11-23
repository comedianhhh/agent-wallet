"""Getting authorized money out the door, and proving afterwards that it went where the ledger says.

A payment in 'processing' has its money in the user's hold account and is waiting on the rail.
It is its own outbox row: whoever finishes it (the inline attempt right after pay(), the worker,
or reconciliation) follows the same three steps:

  claim   one UPDATE pushes next_attempt_at forward (a lease). Only one process wins the row; if it
          dies mid-call, the lease runs out and someone else picks it up.
  call    rail.submit(idempotency_key = payment id). Outside any DB transaction: no lock is held
          while we wait on the network. Calling twice is safe; the rail dedupes on the key.
  apply   in one transaction, with the payment row locked and still 'processing':
            succeeded -> capture: hold -> merchant, status settled
            failed    -> release: hold -> wallet, status failed
            timeout   -> nothing moves; status stays processing, retry later with backoff

The rule that keeps money safe: a timeout is never treated as a failure. Releasing the hold on a
timeout would hand the user money the merchant may already have.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg

from .rails import Rail, RailResult, RailTimeout
from .wallet import PENDING_TTL, _payment_view, _post, _release

LEASE = timedelta(seconds=30)
MAX_ATTEMPTS = 8  # after this the payment is left 'processing' and flagged by reconciliation


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** attempts))


def _claim(conn: psycopg.Connection, payment_id: UUID) -> dict | None:
    return conn.execute(
        """UPDATE payments SET attempts = attempts + 1, next_attempt_at = now() + %s
           WHERE id = %s AND status = 'processing' AND attempts < %s
             AND (next_attempt_at IS NULL OR next_attempt_at <= now())
           RETURNING *""",
        (LEASE, payment_id, MAX_ATTEMPTS),
    ).fetchone()


def _apply(conn: psycopg.Connection, payment_id: UUID, result: RailResult) -> dict:
    with conn.transaction():
        p = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()
        if p["status"] != "processing":
            return p  # someone else already applied the (same, idempotent) result
        if result.status == "succeeded":
            hold = conn.execute(
                "SELECT h.id FROM accounts w JOIN accounts h ON h.user_id = w.user_id AND h.kind = 'hold'"
                " WHERE w.id = %s",
                (p["wallet_id"],),
            ).fetchone()["id"]
            merchant = conn.execute(
                "SELECT account_id FROM merchants WHERE id = %s", (p["merchant_id"],)
            ).fetchone()["account_id"]
            _post(conn, "capture", p["id"], [(hold, -p["amount_cents"]), (merchant, p["amount_cents"])])
            p = conn.execute(
                """UPDATE payments SET status = 'settled', settled_at = now(), rail_ref = %s,
                       next_attempt_at = NULL, last_error = '' WHERE id = %s RETURNING *""",
                (result.rail_ref, payment_id),
            ).fetchone()
        else:
            _release(conn, p, "failed", result.reason or "rail declined")
            conn.execute("UPDATE payments SET rail_ref = %s WHERE id = %s", (result.rail_ref, payment_id))
            p = conn.execute("SELECT * FROM payments WHERE id = %s", (payment_id,)).fetchone()
        return p


def _send(conn: psycopg.Connection, rail: Rail, p: dict) -> dict:
    try:
        result = rail.submit(str(p["id"]), p["merchant_id"], p["amount_cents"])
    except RailTimeout as e:
        # Outcome unknown. Keep the hold, try again later.
        return conn.execute(
            "UPDATE payments SET last_error = %s, next_attempt_at = now() + %s WHERE id = %s RETURNING *",
            (f"rail timeout ({e}); outcome unknown, will retry", _backoff(p["attempts"]), p["id"]),
        ).fetchone()
    return _apply(conn, p["id"], result)


def attempt(conn: psycopg.Connection, rail: Rail, payment_id: UUID) -> dict:
    """Try to finish one payment now (used right after pay() and confirm()). Returns its view."""
    p = _claim(conn, payment_id)
    if p is None:  # not processing any more, or someone else holds the lease
        p = conn.execute("SELECT * FROM payments WHERE id = %s", (payment_id,)).fetchone()
        return _payment_view(p)
    return _payment_view(_send(conn, rail, p))


def run_worker_once(conn: psycopg.Connection, rail: Rail, batch: int = 50) -> dict[str, int]:
    """One pass: expire stale pending payments (release their holds), then finish due 'processing'
    payments. Safe to run in many processes at once (SKIP LOCKED + leases + rail idempotency)."""
    expired = 0
    with conn.transaction():
        for p in conn.execute(
            """SELECT * FROM payments WHERE status = 'pending_confirmation' AND created_at <= now() - %s
               ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED""",
            (PENDING_TTL, batch),
        ).fetchall():
            _release(conn, p, "expired", "user did not approve in time")
            expired += 1
        conn.execute("DELETE FROM rate_windows WHERE window_start < now() - interval '1 hour'")

    claimed = conn.execute(
        """UPDATE payments SET attempts = attempts + 1, next_attempt_at = now() + %s
           WHERE id IN (SELECT id FROM payments
                        WHERE status = 'processing' AND attempts < %s
                          AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                        ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED)
           RETURNING *""",
        (LEASE, MAX_ATTEMPTS, batch),
    ).fetchall()
    out = {"expired": expired, "claimed": len(claimed), "settled": 0, "failed": 0, "still_unknown": 0}
    for p in claimed:
        status = _send(conn, rail, p)["status"]
        out[{"settled": "settled", "failed": "failed"}.get(status, "still_unknown")] += 1
    return out


def reconcile(conn: psycopg.Connection, rail: Rail) -> dict[str, Any]:
    """Compare every payment the rail could know about with the rail's settlement report.

    'processing' payments the rail has a final answer for are finished here (same _apply path as
    the worker). Everything else that disagrees is a finding for a human: it means money and
    records have split, which no retry can fix."""
    report = {t["idempotency_key"]: t for t in rail.transfers()}
    run_id = uuid4()
    issues: list[tuple[UUID | None, str | None, str, str]] = []
    healed = 0

    payments = conn.execute(
        "SELECT * FROM payments WHERE status IN ('processing', 'settled', 'failed')"
    ).fetchall()
    seen: set[str] = set()
    for p in payments:
        key = str(p["id"])
        t = report.get(key)
        seen.add(key)
        if t is not None and (t["amount_cents"] != p["amount_cents"] or t["merchant_id"] != p["merchant_id"]):
            issues.append((p["id"], key, "amount_or_merchant_mismatch",
                           f"ledger {p['merchant_id']} {p['amount_cents']} vs rail {t['merchant_id']} {t['amount_cents']}"))
            continue
        if p["status"] == "processing":
            if t is not None:
                _apply(conn, p["id"], RailResult(t["status"], t["rail_ref"]))
                healed += 1
            elif p["attempts"] >= MAX_ATTEMPTS:
                issues.append((p["id"], key, "stuck", f"{p['attempts']} attempts, rail has no record"))
        elif p["status"] == "settled" and (t is None or t["status"] != "succeeded"):
            issues.append((p["id"], key, "settled_but_rail_did_not_pay",
                           "merchant was credited in the ledger but the rail shows no successful transfer"))
        elif p["status"] == "failed" and t is not None and t["status"] == "succeeded":
            issues.append((p["id"], key, "rail_paid_but_ledger_released",
                           "rail paid the merchant but the hold went back to the user: money is out twice"))

    for key, t in report.items():
        if key not in seen:
            issues.append((None, key, "unknown_rail_transfer",
                           f"rail has {t['status']} {t['amount_cents']} to {t['merchant_id']} with no matching payment"))

    with conn.transaction():
        for pid, key, kind, detail in issues:
            conn.execute(
                "INSERT INTO recon_issues (run_id, payment_id, rail_key, kind, detail) VALUES (%s, %s, %s, %s, %s)",
                (run_id, pid, key, kind, detail),
            )
    return {
        "run_id": str(run_id),
        "checked_payments": len(payments),
        "rail_transfers": len(report),
        "healed_processing": healed,
        "issues": [{"payment_id": str(p) if p else None, "rail_key": k, "kind": kd, "detail": d}
                   for p, k, kd, d in issues],
        "ok": not issues,
    }
