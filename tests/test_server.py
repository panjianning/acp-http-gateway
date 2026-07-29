# Copyright 2026 acp-http-gateway authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for the ACP HTTP gateway.

Uses a fake agent subprocess (``python3 -c "..."``) that echoes
JSON-RPC responses.  Tests run against a real aiohttp server on a
random port.
"""

from __future__ import annotations


import aiohttp
import pytest

from acp_http_gateway.auth import BearerTokenValidator, NoAuthValidator
from acp_http_gateway.server import (
    HEADER_CONNECTION_ID,
    HEADER_SESSION_ID,
    create_app,
)

# ── Fake agent ─────────────────────────────────────────────────────

_FAKE_AGENT = r"""
import sys, json
line = sys.stdin.readline()
req = json.loads(line)
method = req.get("method","")
if method == "initialize":
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"protocolVersion":1,"agentInfo":{"name":"fake","version":"1.0"}}}), flush=True)
elif method == "session/new":
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"sessionId":"sess_test"}}), flush=True)
elif method == "session/prompt":
    print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":req["params"]["sessionId"],"message":{"role":"assistant","content":"hello"}}}), flush=True)
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"sessionId":req["params"]["sessionId"],"stopReason":"end_turn"}}), flush=True)
else:
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{}}), flush=True)
"""


def _fake_cmd() -> list[str]:
    return ["python3", "-c", _FAKE_AGENT]


# ── Test fixture ───────────────────────────────────────────────────


@pytest.fixture
async def base_url():
    """Start a test server on a random port, return its base URL."""
    app = create_app(_fake_cmd(), auth_validator=NoAuthValidator(), cors_origin="*")

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Extract the assigned port
    for sock in site._server.sockets:
        port = sock.getsockname()[1]
        break
    else:
        port = 0

    url = f"http://127.0.0.1:{port}"
    yield url

    await runner.cleanup()


@pytest.fixture
async def session():
    """aiohttp client session."""
    async with aiohttp.ClientSession() as s:
        yield s


# ── initialize helper ────────────────────────────────────────────────


async def _initialize(session: aiohttp.ClientSession, base_url: str) -> str:
    """Perform initialize handshake, return connection_id."""
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    ) as resp:
        assert resp.status == 200
        conn_id = resp.headers.get(HEADER_CONNECTION_ID)
        assert conn_id is not None
        return conn_id


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health(base_url, session):
    """GET /health returns 200."""
    async with session.get(f"{base_url}/health") as resp:
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_initialize_returns_connection_id(base_url, session):
    """initialize returns 200 + Acp-Connection-Id header."""
    conn_id = await _initialize(session, base_url)
    assert len(conn_id) == 16


@pytest.mark.asyncio
async def test_initialize_sets_cookie(base_url, session):
    """initialize sets the acp_conn cookie."""
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    ) as resp:
        assert resp.status == 200
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "acp_conn=" in set_cookie


@pytest.mark.asyncio
async def test_initialize_patches_connection_id(base_url, session):
    """Response result.connectionId matches the header."""
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    ) as resp:
        assert resp.status == 200
        conn_id = resp.headers[HEADER_CONNECTION_ID]
        data = await resp.json()
        assert data["result"]["connectionId"] == conn_id


@pytest.mark.asyncio
async def test_bad_content_type_415(base_url, session):
    """415 for non-JSON POST."""
    async with session.post(f"{base_url}/acp", data="not json") as resp:
        assert resp.status == 415


@pytest.mark.asyncio
async def test_missing_conn_id_404(base_url, session):
    """404 when POST without Acp-Connection-Id."""
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "session/new",
            "id": 2,
            "params": {"cwd": "/tmp"},
        },
    ) as resp:
        assert resp.status == 404


@pytest.mark.asyncio
async def test_unknown_conn_id_404(base_url, session):
    """404 when POST with unknown Acp-Connection-Id."""
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "session/new",
            "id": 2,
            "params": {"cwd": "/tmp"},
        },
        headers={HEADER_CONNECTION_ID: "deadbeef00000000"},
    ) as resp:
        assert resp.status == 404


@pytest.mark.asyncio
async def test_session_new_202(base_url, session):
    """202 for session/new with valid connection."""
    conn_id = await _initialize(session, base_url)
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "session/new",
            "id": 2,
            "params": {"cwd": "/tmp"},
        },
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 202


@pytest.mark.asyncio
async def test_session_prompt_202(base_url, session):
    """202 for session/prompt."""
    conn_id = await _initialize(session, base_url)
    # session/new first
    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "session/new",
            "id": 2,
            "params": {"cwd": "/tmp"},
        },
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 202

    async with session.post(
        f"{base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "id": 3,
            "params": {
                "sessionId": "sess_test",
                "prompt": [{"type": "text", "text": "hello"}],
            },
        },
        headers={
            HEADER_CONNECTION_ID: conn_id,
            HEADER_SESSION_ID: "sess_test",
        },
    ) as resp:
        assert resp.status == 202


@pytest.mark.asyncio
async def test_delete_conn_202(base_url, session):
    """202 for DELETE with valid connection."""
    conn_id = await _initialize(session, base_url)
    async with session.delete(
        f"{base_url}/acp",
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 202


@pytest.mark.asyncio
async def test_delete_unknown_conn_404(base_url, session):
    """404 for DELETE with unknown connection."""
    async with session.delete(
        f"{base_url}/acp",
        headers={HEADER_CONNECTION_ID: "deadbeef00000000"},
    ) as resp:
        assert resp.status == 404


@pytest.mark.asyncio
async def test_delete_twice_404(base_url, session):
    """Second DELETE is 404."""
    conn_id = await _initialize(session, base_url)
    async with session.delete(
        f"{base_url}/acp",
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 202
    async with session.delete(
        f"{base_url}/acp",
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 404


@pytest.mark.asyncio
async def test_sse_accept_header_required(base_url, session):
    """406 without Accept: text/event-stream."""
    conn_id = await _initialize(session, base_url)
    async with session.get(
        f"{base_url}/acp",
        headers={HEADER_CONNECTION_ID: conn_id},
    ) as resp:
        assert resp.status == 406


@pytest.mark.asyncio
async def test_sse_content_type(base_url, session):
    """SSE response has Content-Type: text/event-stream."""
    conn_id = await _initialize(session, base_url)
    async with session.get(
        f"{base_url}/acp",
        headers={
            "Accept": "text/event-stream",
            HEADER_CONNECTION_ID: conn_id,
        },
    ) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"


@pytest.mark.asyncio
async def test_sse_unknown_conn_404(base_url, session):
    """SSE returns 404 for unknown connection."""
    async with session.get(
        f"{base_url}/acp",
        headers={
            "Accept": "text/event-stream",
            HEADER_CONNECTION_ID: "deadbeef00000000",
        },
    ) as resp:
        assert resp.status == 404


@pytest.mark.asyncio
async def test_cors_preflight(base_url, session):
    """OPTIONS returns 204 with CORS headers."""
    async with session.options(f"{base_url}/acp") as resp:
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.asyncio
async def test_query_param_resolve_connection(base_url, session):
    """Connection resolved via ?connection_id=xxx query param."""
    conn_id = await _initialize(session, base_url)
    async with session.get(
        f"{base_url}/acp?connection_id={conn_id}",
        headers={"Accept": "text/event-stream"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"


# ── Auth tests (separate server) ───────────────────────────────────


@pytest.fixture
async def auth_base_url():
    """Start a test server with bearer token auth."""
    app = create_app(
        _fake_cmd(),
        auth_validator=BearerTokenValidator("sk-test"),
    )
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    for sock in site._server.sockets:
        port = sock.getsockname()[1]
        break
    else:
        port = 0
    url = f"http://127.0.0.1:{port}"
    yield url
    await runner.cleanup()


@pytest.mark.asyncio
async def test_auth_unauthorized(auth_base_url, session):
    """401 without bearer token."""
    async with session.post(
        f"{auth_base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    ) as resp:
        assert resp.status == 401


@pytest.mark.asyncio
async def test_auth_authorized(auth_base_url, session):
    """200 with correct bearer token."""
    async with session.post(
        f"{auth_base_url}/acp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers={"Authorization": "Bearer sk-test"},
    ) as resp:
        assert resp.status == 200


@pytest.mark.asyncio
async def test_sse_cors_header(base_url, session):
    """GET /acp returns Access-Control-Allow-Origin."""
    conn_id = await _initialize(session, base_url)
    async with session.get(
        f"{base_url}/acp",
        headers={
            "Accept": "text/event-stream",
            HEADER_CONNECTION_ID: conn_id,
        },
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
