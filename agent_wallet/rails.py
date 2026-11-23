"""The payment rail: whatever actually moves money to the merchant (card network, gift-card issuer,
bank transfer). The wallet only knows this interface.

The hard part of any rail is not the happy path, it's the third outcome: you asked, and you don't
know what happened (timeout, dropped connection, 502). The wallet's answer is that every submit
carries an idempotency key (the payment id), so asking again is always safe, and the rail's
settlement report is the final word during reconciliation.

FakeRail stands in for a real provider. It keeps its own records in a separate Postgres schema
(`fake_rail`), written on its own connection, so it can never share a transaction with the wallet:
the same position a real provider's database is in. Faults are injected per call.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class RailResult:
    status: Literal["succeeded", "failed"]
    rail_ref: str
    reason: str = ""


class RailTimeout(Exception):
    """We don't know whether the transfer happened. Never treat this as failed."""


class Rail(Protocol):
    def submit(self, idempotency_key: str, merchant_id: str, amount_cents: int) -> RailResult: ...

    def transfers(self) -> list[dict]:
        """Settlement report: every transfer the rail has, {idempotency_key, merchant_id, amount_cents, status, rail_ref}."""
        ...


Fault = Literal["ok", "decline", "timeout_before", "timeout_after"]

FAKE_RAIL_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS fake_rail;
CREATE TABLE IF NOT EXISTS fake_rail.transfers (
    idempotency_key  text PRIMARY KEY,
    merchant_id      text NOT NULL,
    amount_cents     bigint NOT NULL,
    status           text NOT NULL CHECK (status IN ('succeeded', 'failed')),
    rail_ref         text NOT NULL,
    reason           text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now()
);
"""


class FakeRail:
    """A provider with a real idempotency contract: the same key always gets the same result,
    and a transfer is never made twice.

    faults(idempotency_key) -> Fault decides what happens on each call:
      ok              transfer succeeds
      decline         provider refuses (recorded as failed, so a retry gets the same refusal)
      timeout_before  request never reached the provider: nothing recorded, caller sees a timeout
      timeout_after   provider made the transfer, but the response was lost: caller sees a timeout
    """

    def __init__(self, dsn: str, faults: Callable[[str], Fault] | None = None):
        self.dsn = dsn
        self.faults = faults
        self._local = threading.local()

    def _conn(self) -> psycopg.Connection:
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            c = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            self._local.conn = c
        return c

    def ensure_schema(self) -> None:
        self._conn().execute(FAKE_RAIL_SCHEMA)

    def submit(self, idempotency_key: str, merchant_id: str, amount_cents: int) -> RailResult:
        fault = self.faults(idempotency_key) if self.faults else "ok"
        if fault == "timeout_before":
            raise RailTimeout("connect timeout")
        c = self._conn()
        status = "failed" if fault == "decline" else "succeeded"
        reason = "merchant declined" if fault == "decline" else ""
        c.execute(
            """INSERT INTO fake_rail.transfers (idempotency_key, merchant_id, amount_cents, status, rail_ref, reason)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
            (idempotency_key, merchant_id, amount_cents, status, "fr_" + secrets.token_hex(6), reason),
        )
        row = c.execute("SELECT * FROM fake_rail.transfers WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
        if fault == "timeout_after":
            raise RailTimeout("read timeout")
        return RailResult(row["status"], row["rail_ref"], row["reason"])

    def transfers(self) -> list[dict]:
        return self._conn().execute(
            "SELECT idempotency_key, merchant_id, amount_cents, status, rail_ref FROM fake_rail.transfers"
        ).fetchall()


def rail_from_env() -> FakeRail:
    """AGENT_WALLET_RAIL=fake (the only one so far). The fake rail lives in the same Postgres,
    in its own schema, reached through its own connection."""
    import os

    from .db import dsn

    kind = os.environ.get("AGENT_WALLET_RAIL", "fake")
    if kind != "fake":
        raise SystemExit(f"unknown AGENT_WALLET_RAIL={kind!r}; only 'fake' exists")
    rail = FakeRail(dsn())
    rail.ensure_schema()
    return rail
