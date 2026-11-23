"""Admin CLI: the user's and the operator's side. Everything the agent must NOT be able to do
(fund, set limits, issue/revoke keys, approve pending payments, refund) lives here.

  agent-wallet init-db
  agent-wallet add-user alan
  agent-wallet add-merchant coffee-shop "Coffee Shop"
  agent-wallet fund <user_id> 200.00
  agent-wallet issue-key <user_id> --label shopping-agent --per-payment 50 --daily 100 \
       --merchants coffee-shop,bookstore [--confirm-above 40] [--expires-days 30] [--scope read]
  agent-wallet revoke-key <key_id>
  agent-wallet pending
  agent-wallet confirm <payment_id> | decline <payment_id>
  agent-wallet refund <payment_id> 12.50 --idempotency-key r1 [--reason "damaged"]
  agent-wallet show <user_id>
  agent-wallet verify                 # ledger audit
  agent-wallet worker [--once]        # finish in-flight payments, expire stale pending ones
  agent-wallet reconcile              # compare the ledger with the rail's settlement report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from uuid import UUID

from . import settlement, wallet
from .db import connect, init_schema, load_env_file
from .rails import rail_from_env


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("AGENT_WALLET_ENV_FILE"):
        load_env_file(os.environ["AGENT_WALLET_ENV_FILE"])
    elif os.path.exists(".env"):
        load_env_file(".env")

    p = argparse.ArgumentParser(prog="agent-wallet")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    s = sub.add_parser("add-user"); s.add_argument("name")
    s = sub.add_parser("add-merchant"); s.add_argument("merchant_id"); s.add_argument("name")
    s = sub.add_parser("fund"); s.add_argument("user_id", type=UUID); s.add_argument("amount")
    s = sub.add_parser("issue-key")
    s.add_argument("user_id", type=UUID)
    s.add_argument("--label", required=True)
    s.add_argument("--scope", choices=["read", "pay"], default="pay")
    s.add_argument("--per-payment", required=True)
    s.add_argument("--daily", required=True)
    s.add_argument("--merchants", default="")
    s.add_argument("--confirm-above")
    s.add_argument("--expires-days", type=int)
    s.add_argument("--rate-per-min", type=int, default=60)
    s = sub.add_parser("revoke-key"); s.add_argument("key_id", type=UUID)
    sub.add_parser("pending")
    s = sub.add_parser("confirm"); s.add_argument("payment_id", type=UUID)
    s = sub.add_parser("decline"); s.add_argument("payment_id", type=UUID)
    s = sub.add_parser("refund")
    s.add_argument("payment_id", type=UUID); s.add_argument("amount")
    s.add_argument("--idempotency-key", required=True); s.add_argument("--reason", default="")
    s = sub.add_parser("show"); s.add_argument("user_id", type=UUID)
    sub.add_parser("verify")
    s = sub.add_parser("worker"); s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=float, default=2.0)
    sub.add_parser("reconcile")
    a = p.parse_args(argv)

    conn = connect()
    init_schema(conn)
    try:
        match a.cmd:
            case "init-db":
                _print({"ok": True})
            case "add-user":
                _print(wallet.create_user(conn, a.name))
            case "add-merchant":
                _print(wallet.create_merchant(conn, a.merchant_id, a.name))
            case "fund":
                _print({"balance": wallet.fmt(wallet.fund(conn, a.user_id, wallet.parse_amount(a.amount)))})
            case "issue-key":
                k = wallet.issue_key(
                    conn, a.user_id, label=a.label, scope=a.scope,
                    per_payment_cap_cents=wallet.parse_amount(a.per_payment),
                    daily_cap_cents=wallet.parse_amount(a.daily),
                    merchants=[m for m in a.merchants.split(",") if m],
                    confirm_above_cents=wallet.parse_amount(a.confirm_above) if a.confirm_above else None,
                    expires_in=timedelta(days=a.expires_days) if a.expires_days else None,
                    rate_limit_per_min=a.rate_per_min,
                )
                _print({"key_id": k.key_id, "api_key": k.secret,
                        "note": "shown once; put it in the MCP host config as AGENT_WALLET_KEY"})
            case "revoke-key":
                wallet.revoke_key(conn, a.key_id); _print({"revoked": a.key_id})
            case "pending":
                _print(conn.execute(
                    "SELECT id, merchant_id, amount_cents, memo, created_at FROM payments"
                    " WHERE status = 'pending_confirmation' AND created_at > now() - %s ORDER BY created_at",
                    (wallet.PENDING_TTL,),
                ).fetchall())
            case "confirm":
                _print(wallet.confirm(conn, a.payment_id, rail=rail_from_env()))
            case "decline":
                _print(wallet.decline(conn, a.payment_id))
            case "refund":
                _print(wallet.refund(conn, a.payment_id, wallet.parse_amount(a.amount), a.idempotency_key, a.reason))
            case "show":
                w = wallet.wallet_of(conn, a.user_id)
                _print({
                    "balance": wallet.fmt(wallet.balance(conn, w)),
                    "keys": conn.execute(
                        "SELECT id, label, key_prefix, scope, per_payment_cap_cents, daily_cap_cents,"
                        " confirm_above_cents, expires_at, revoked_at FROM agent_keys WHERE user_id = %s",
                        (a.user_id,),
                    ).fetchall(),
                    "recent_payments": conn.execute(
                        "SELECT id, merchant_id, amount_cents, refunded_cents, status, memo, created_at"
                        " FROM payments WHERE wallet_id = %s ORDER BY created_at DESC LIMIT 20", (w,),
                    ).fetchall(),
                    "recent_denials": conn.execute(
                        "SELECT d.created_at, d.merchant_id, d.amount_cents, d.reason FROM decisions d"
                        " JOIN agent_keys k ON k.id = d.key_id WHERE k.user_id = %s AND d.outcome = 'denied'"
                        " ORDER BY d.created_at DESC LIMIT 20", (a.user_id,),
                    ).fetchall(),
                })
            case "verify":
                r = wallet.verify_ledger(conn)
                _print(r)
                return 0 if r["ok"] else 1
            case "worker":
                rail = rail_from_env()
                while True:
                    r = settlement.run_worker_once(conn, rail)
                    if a.once or r["claimed"] or r["expired"]:
                        _print(r)
                    if a.once:
                        break
                    time.sleep(a.interval)
            case "reconcile":
                r = settlement.reconcile(conn, rail_from_env())
                _print(r)
                return 0 if r["ok"] else 1
    except wallet.WalletError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
