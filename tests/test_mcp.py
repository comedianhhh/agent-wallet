"""The MCP layer: real stdio subprocess the way Claude Desktop starts it, and readable error codes."""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import Client

from agent_wallet.mcp_server import Conn, build_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_stdio_end_to_end(world, dsn):
    """Spawn the server as a subprocess over stdio and pay through it. Anything printed to stdout
    would corrupt the JSON-RPC stream and fail this test."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_wallet.mcp_server"],
        cwd=ROOT,
        env={**os.environ, "AGENT_WALLET_DSN": dsn, "AGENT_WALLET_KEY": world["key"]},
    )

    async def run():
        with open(os.devnull, "w") as quiet:
            async with stdio_client(params, errlog=quiet) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    w = await s.call_tool("get_wallet", {})
                    assert json.loads(w.content[0].text)["balance_cents"] == 50_000
                    p = await s.call_tool("pay", {"merchant_id": "coffee", "amount_cents": 450,
                                                  "idempotency_key": "stdio-1", "memo": "drip coffee"})
                    assert not p.is_error and json.loads(p.content[0].text)["status"] == "settled"
                    bad = await s.call_tool("pay", {"merchant_id": "attacker", "amount_cents": 450,
                                                    "idempotency_key": "stdio-2"})
                    assert bad.is_error and "MERCHANT_NOT_ALLOWED:" in bad.content[0].text

    anyio.run(run)


def test_missing_key_is_a_clear_error(world):
    async def run():
        async with Client(build_server(Conn(lambda: world["conn"]), None)) as c:
            r = await c.call_tool("get_wallet", {})
            assert r.is_error and "AGENT_WALLET_KEY" in r.content[0].text

    anyio.run(run)


def test_pending_flow_through_tools(world):
    from agent_wallet import wallet

    from .conftest import RAIL

    c = world["conn"]
    k = wallet.issue_key(c, world["user"]["user_id"], label="ask-me", per_payment_cap_cents=5_000,
                         daily_cap_cents=10_000, merchants=["books"], confirm_above_cents=0)

    async def run():
        async with Client(build_server(Conn(lambda: c), k.secret)) as client:
            r = await client.call_tool("pay", {"merchant_id": "books", "amount_cents": 2_999,
                                               "idempotency_key": "b1"})
            pid = json.loads(r.content[0].text)["payment_id"]
            assert json.loads(r.content[0].text)["status"] == "pending_confirmation"
            wallet.confirm(c, pid, rail=RAIL)  # the user taps Approve in their app
            s = await client.call_tool("get_payment", {"payment_id": pid})
            assert json.loads(s.content[0].text)["status"] == "settled"

    anyio.run(run)
