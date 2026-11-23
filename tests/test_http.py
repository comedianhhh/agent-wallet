"""The remote server over real HTTP (uvicorn on a free port): bearer auth, one process serving many
agents each confined to its own key, per-key rate limits, and parallel tool calls on a pool."""

from __future__ import annotations

import json
import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from agent_wallet import wallet
from agent_wallet.http_server import build_http_app
from agent_wallet.mcp_server import PoolConn, build_server

from .conftest import RAIL

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}


@pytest.fixture
def server_url(dsn):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pool = PoolConn(dsn, max_size=10)
    app = build_http_app(pool, RAIL, public_url=f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)
    pool.pool.close()


async def session_calls(url: str, key: str, calls: list[tuple[str, dict]]) -> list[tuple[bool, str]]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    out = []
    async with http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                for name, args in calls:
                    r = await s.call_tool(name, args)
                    out.append((r.is_error, r.content[0].text if r.content else ""))
    return out


def raw_post(url: str, token: str | None) -> httpx2.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx2.post(f"{url}/mcp", headers=headers, json=INIT)


def second_user(conn, name="bob", funds=20_000, **key_kw):
    u = wallet.create_user(conn, name)
    wallet.fund(conn, u["user_id"], funds)
    kw = dict(label=name, per_payment_cap_cents=5_000, daily_cap_cents=10_000, merchants=["coffee"]) | key_kw
    return u, wallet.issue_key(conn, u["user_id"], **kw)


def test_requests_without_a_valid_key_are_refused_before_any_tool(server_url, world):
    for token in (None, "awk_not-a-real-key", "awk_", "Bearer", "sk_live_xyz"):
        r = raw_post(server_url, token)
        assert r.status_code == 401, (token, r.status_code)
        assert "Bearer" in r.headers.get("www-authenticate", "")
    assert raw_post(server_url, world["key"]).status_code == 200


def test_revoked_key_is_refused_on_the_next_request(server_url, world):
    assert raw_post(server_url, world["key"]).status_code == 200
    wallet.revoke_key(world["conn"], world["key_id"])
    assert raw_post(server_url, world["key"]).status_code == 401


def test_pay_over_http(server_url, world):
    r = anyio.run(session_calls, server_url, world["key"], [
        ("pay", {"merchant_id": "coffee", "amount_cents": 450, "idempotency_key": "h1"}),
        ("pay", {"merchant_id": "attacker", "amount_cents": 450, "idempotency_key": "h2"}),
        ("get_wallet", {}),
    ])
    assert not r[0][0] and json.loads(r[0][1])["status"] == "settled"
    assert r[1][0] and "MERCHANT_NOT_ALLOWED:" in r[1][1]
    assert json.loads(r[2][1])["balance_cents"] == 50_000 - 450


def test_one_server_many_agents_each_sees_only_its_own_wallet(server_url, world):
    """Interleaved, concurrent requests from two agents. If the request's key ever leaked into
    another request (a contextvar bug, a shared connection), a balance would come back wrong."""
    _, bob = second_user(world["conn"])
    expected = {world["key"]: 50_000, bob.secret: 20_000}
    results: dict[str, list] = {}

    async def agent(key):
        results[key] = await session_calls(server_url, key, [("get_wallet", {})] * 15)

    async def both():
        async with anyio.create_task_group() as tg:
            for key in (world["key"], bob.secret) * 3:
                tg.start_soon(agent, key)

    anyio.run(both)
    for key, calls in results.items():
        assert {json.loads(text)["balance_cents"] for _, text in calls} == {expected[key]}


def test_rate_limit_is_per_key(server_url, world):
    _, slow = second_user(world["conn"], "slowpoke", rate_limit_per_min=5)
    r = anyio.run(session_calls, server_url, slow.secret, [("get_wallet", {})] * 7)
    assert [e for e, _ in r] == [False] * 5 + [True] * 2
    assert "RATE_LIMITED:" in r[5][1] and "retry in" in r[5][1]
    # Another key on the same server is unaffected.
    other = anyio.run(session_calls, server_url, world["key"], [("get_wallet", {})])
    assert not other[0][0]
    # The next minute starts fresh.
    world["conn"].execute("UPDATE rate_windows SET window_start = window_start - interval '1 minute'")
    assert not anyio.run(session_calls, server_url, slow.secret, [("get_wallet", {})])[0][0]


def test_rate_limit_stops_a_looping_agent_before_it_floods_the_log(world):
    c = world["conn"]
    _, k = second_user(c, "looper", rate_limit_per_min=10)
    for i in range(50):
        try:
            wallet.check_rate(c, k.secret)
            try:
                wallet.pay(c, k.secret, "attacker", 100, f"x{i}")
            except wallet.WalletError:
                pass
        except wallet.WalletError as e:
            assert e.code == "RATE_LIMITED"
    n = c.execute("SELECT COUNT(*) AS n FROM decisions d JOIN agent_keys k ON k.id = d.key_id"
                  " WHERE k.label = 'looper'").fetchone()["n"]
    assert n == 10


def test_parallel_tool_calls_on_a_pool_still_respect_the_daily_cap(dsn, world):
    """The SDK runs sync tools in threads, so one agent firing tool calls in parallel really does
    run them at the same time, each on its own pooled connection."""
    pool = PoolConn(dsn, max_size=10)
    server = build_server(pool, world["key"], RAIL)

    async def go():
        out = []
        async with Client(server) as client:
            async def one(i):
                r = await client.call_tool("pay", {"merchant_id": "coffee", "amount_cents": 3_000,
                                                   "idempotency_key": f"par-{i}"})
                out.append("ok" if not r.is_error else r.content[0].text)
            async with anyio.create_task_group() as tg:
                for i in range(20):
                    tg.start_soon(one, i)
        return out

    try:
        out = anyio.run(go)
    finally:
        pool.pool.close()
    assert out.count("ok") == 3
    assert sum("OVER_DAILY_CAP" in o for o in out) == 17
    assert wallet.verify_ledger(world["conn"])["ok"]
