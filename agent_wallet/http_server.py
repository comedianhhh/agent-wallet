"""agent-wallet as a remote MCP server (Streamable HTTP), for agents that don't run on the user's
machine.

Auth: each request carries `Authorization: Bearer awk_...`, the same API key the stdio server reads
from its env. The SDK's bearer middleware calls WalletTokenVerifier on every request; an unknown,
revoked or expired key gets 401 before any tool runs. The tools then read the key of *this request*
from the auth contextvar, so one server process serves many agents, each confined to its own key.

The keys are issued by the wallet itself (`agent-wallet issue-key`), so this server is an OAuth
"resource server" that accepts pre-issued tokens. It does not run an OAuth authorization flow.

Stateless mode with JSON responses: no MCP session lives in the server's memory, so any replica
behind a load balancer can take any request. All state is in Postgres (including rate limits).

Run:  AGENT_WALLET_DSN=... uv run agent-wallet-http      (listens on 127.0.0.1:8765 by default)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from starlette.applications import Starlette

from . import wallet
from .db import connect, dsn, init_schema, load_env_file
from .mcp_server import PoolConn, build_server

log = logging.getLogger("agent_wallet.http")


class WalletTokenVerifier:
    def __init__(self, conn: PoolConn):
        self.conn = conn

    async def verify_token(self, token: str) -> AccessToken | None:
        def check() -> AccessToken | None:
            try:
                k = self.conn.run(lambda c: wallet.authenticate(c, token))
            except wallet.WalletError:
                return None
            return AccessToken(
                token=token,
                client_id=str(k["key_id"]),
                subject=str(k["user_id"]),
                scopes=[k["scope"]],
                expires_at=int(k["expires_at"].timestamp()) if k["expires_at"] else None,
            )

        return await anyio.to_thread.run_sync(check)


def _request_key() -> str | None:
    t = get_access_token()
    return t.token if t else None


def build_http_app(conn: PoolConn, rail: Any, public_url: str, host: str = "127.0.0.1") -> Starlette:
    server = build_server(
        conn,
        _request_key,
        rail,
        token_verifier=WalletTokenVerifier(conn),
        auth=AuthSettings(
            issuer_url=public_url,  # keys are issued by this wallet; no separate authorization server
            resource_server_url=f"{public_url}/mcp",
            validate_token_resource=False,  # pre-issued keys carry no audience; the DB lookup is the check
        ),
    )
    return server.streamable_http_app(stateless_http=True, json_response=True, host=host)


def main() -> None:
    import uvicorn

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if os.environ.get("AGENT_WALLET_ENV_FILE"):
        load_env_file(os.environ["AGENT_WALLET_ENV_FILE"])
    host = os.environ.get("AGENT_WALLET_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_WALLET_HTTP_PORT", "8765"))
    public_url = os.environ.get("AGENT_WALLET_PUBLIC_URL", f"http://{host}:{port}")
    with connect() as c:
        init_schema(c)
    from .rails import rail_from_env

    app = build_http_app(PoolConn(dsn()), rail_from_env(), public_url, host=host)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
