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
"""aiohttp application implementing the ACP Streamable HTTP endpoint.

Provides the ``/acp`` endpoint that handles:

- **POST** — Send JSON-RPC messages to the agent subprocess.
- **GET** — Open an SSE stream for server→client messages.
- **DELETE** — Terminate the connection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from .auth import AuthValidator, NoAuthValidator
from .bridge import (
    initialize_agent,
    spawn_agent,
    start_stdout_router,
    write_to_agent,
)
from .connection import ConnectionStore
from .openai import SessionPool, make_openai_handler
from .sse import sse_response, write_sse_stream

logger = logging.getLogger(__name__)

# Maximum JSON body size for POST requests (10 MB).
MAX_BODY_SIZE = 10 * 1024 * 1024

# ── Header constants (per ACP spec) ─────────────────────────────────

HEADER_CONNECTION_ID = "Acp-Connection-Id"
HEADER_SESSION_ID = "Acp-Session-Id"
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_ACCEPT = "Accept"
COOKIE_CONNECTION = "acp_conn"
VALID_CONTENT_TYPE = "application/json"
VALID_ACCEPT = "text/event-stream"


def _bad_request(reason: str) -> web.Response:
    """Return a 400 response with a JSON error body."""
    return web.json_response(
        {"error": reason},
        status=400,
    )


def _not_found(reason: str) -> web.Response:
    """Return a 404 response with a JSON error body."""
    return web.json_response(
        {"error": reason},
        status=404,
    )


def _unauthorized(reason: str = "Authentication required") -> web.Response:
    """Return a 401 response with a JSON error body."""
    return web.json_response(
        {"error": reason},
        status=401,
    )


def _resolve_connection(
    store: ConnectionStore,
    request: web.Request,
) -> tuple[Any, str | None]:
    """Resolve the connection from request headers or cookie.

    Checks in order:
    1. ``Acp-Connection-Id`` header.
    2. ``acp_conn`` cookie (for browser SSE compatibility).

    Args:
        store: The connection registry.
        request: The incoming HTTP request.

    Returns:
        A tuple of ``(connection | None, source_description)`` where
        *source_description* indicates how the connection was resolved
        (for logging).
    """
    conn_id = request.headers.get(HEADER_CONNECTION_ID)
    if conn_id:
        return store.get(conn_id), f"header({conn_id})"

    # Check query parameter (ACP spec: ?connection_id=xxx for browser SSE)
    conn_id = request.query.get("connection_id")
    if conn_id:
        return store.get(conn_id), f"query({conn_id})"

    cookie = request.cookies.get(COOKIE_CONNECTION)
    if cookie:
        return store.lookup_by_cookie(cookie), f"cookie({cookie})"

    return None, None


def create_app(
    cmd: list[str],
    auth_validator: AuthValidator | None = None,
    *,
    max_capacity: int = 50,
    idle_timeout: float = 300.0,
    cors_origin: str | None = None,
    env: dict[str, str] | None = None,
    enable_openai: bool = False,
    openai_pool_max: int = 20,
    openai_pool_idle: float = 600.0,
) -> web.Application:
    """Create an aiohttp application for the ACP HTTP gateway.

    Args:
        cmd: The agent command as a list of arguments (e.g.
            ``["npx", "pi-acp"]``).
        auth_validator: Pluggable auth validator.  Defaults to
            :class:`NoAuthValidator`.
        max_capacity: Maximum concurrent connections.
        idle_timeout: Seconds of inactivity before a connection is
            eligible for cleanup.
        cors_origin: Value for ``Access-Control-Allow-Origin`` header.
            Set to ``"*"`` to allow all origins (useful for browser
            clients on a different port during development).
        env: Environment variables for the agent subprocess.
        enable_openai: If True, expose ``POST /v1/chat/completions``
            (OpenAI-compatible layer).
        openai_pool_max: Max concurrent sessions in the OpenAI pool.
        openai_pool_idle: Idle timeout (seconds) before a pooled OpenAI
            session is evicted.

    Returns:
        A configured :class:`aiohttp.web.Application`.
    """
    if auth_validator is None:
        auth_validator = NoAuthValidator()

    store = ConnectionStore(idle_timeout=idle_timeout)
    semaphore = asyncio.Semaphore(max_capacity)

    # OpenAI compatibility layer (optional)
    openai_pool = SessionPool(
        max_size=openai_pool_max,
        idle_timeout=openai_pool_idle,
    )
    openai_handler = None
    if enable_openai:
        openai_handler = make_openai_handler(cmd, env, openai_pool)

    async def _handle_post(request: web.Request) -> web.Response:
        """Handle POST /acp — send JSON-RPC to agent."""
        # Validate Content-Type
        ct = request.headers.get(HEADER_CONTENT_TYPE, "")
        if ct.lower() != VALID_CONTENT_TYPE:
            return web.Response(
                status=415,
                text=f"Unsupported Media Type: expected {VALID_CONTENT_TYPE}",
            )

        # Parse body
        try:
            body = await request.json()
        except Exception:
            return _bad_request("Invalid JSON body")

        method = body.get("method", "")
        logger.info("POST /acp method=%s", method)

        # ── initialize (no Acp-Connection-Id yet) ──────────────────
        if method == "initialize":
            # Auth check
            auth_context = await auth_validator.validate(request)
            if auth_context is None:
                return _unauthorized()

            # Enforce capacity
            if semaphore.locked():
                return web.json_response(
                    {"error": "Server at capacity — try again later"},
                    status=503,
                )

            async with semaphore:
                # Spawn agent + handshake
                conn = await spawn_agent(cmd, env=env, auth_context=auth_context)
                store.add(conn)

                try:
                    response = await initialize_agent(conn, body)
                except RuntimeError as exc:
                    await store.remove(conn.connection_id)
                    return web.json_response(
                        {"error": str(exc)},
                        status=502,
                    )

                # Start background stdout router AFTER handshake
                # (avoids racing on stdout.readline between initialize
                #  and the background router)
                _router_task = await start_stdout_router(conn)

                # Return 200 with Acp-Connection-Id header + cookie
                headers = {
                    HEADER_CONNECTION_ID: conn.connection_id,
                    "Set-Cookie": (
                        f"{COOKIE_CONNECTION}={conn.connection_id}; "
                        "HttpOnly; SameSite=Lax; Path=/"
                    ),
                }
                if cors_origin:
                    headers["Access-Control-Allow-Origin"] = cors_origin
                    headers["Access-Control-Expose-Headers"] = (
                        f"{HEADER_CONNECTION_ID}, {HEADER_SESSION_ID}"
                    )

            return web.json_response(response, status=200, headers=headers)

        # ── All other POST methods (require Acp-Connection-Id) ──────
        conn, source = _resolve_connection(store, request)
        if conn is None:
            return _not_found("Unknown Acp-Connection-Id")

        # Check for required session ID on session-scoped methods
        session_methods = {
            "session/new",
            "session/load",
            "session/prompt",
            "session/cancel",
            "session/set_mode",
            "session/set_model",
        }
        if method in session_methods:
            session_id = request.headers.get(HEADER_SESSION_ID) or body.get(
                "params", {}
            ).get("sessionId")
            if not session_id and method not in ("session/new", "session/load"):
                # session/new and session/load don't need Session-Id yet
                pass
            logger.debug("POST session-method=%s session=%s", method, session_id)

        await write_to_agent(conn, body)

        headers = {}
        if cors_origin:
            headers["Access-Control-Allow-Origin"] = cors_origin
        return web.Response(status=202, headers=headers)

    async def _handle_get(request: web.Request) -> web.Response:
        """Handle GET /acp — open an SSE stream."""
        accept = request.headers.get(HEADER_ACCEPT, "")
        if accept.lower() != VALID_ACCEPT:
            return web.Response(
                status=406,
                text=f"Not Acceptable: expected {VALID_ACCEPT}",
            )

        conn, source = _resolve_connection(store, request)
        if conn is None:
            return _not_found("Unknown Acp-Connection-Id")

        # Determine stream scope
        session_id = request.headers.get(HEADER_SESSION_ID)
        if session_id:
            if session_id not in conn.sse_queues:
                logger.debug(
                    "GET session-scoped SSE conn=%s session=%s",
                    conn.connection_id,
                    session_id,
                )
        else:
            logger.debug("GET connection-scoped SSE conn=%s", conn.connection_id)

        # Create SSE queue for this stream
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        conn.sse_queues[session_id] = queue
        done = asyncio.Event()

        resp = await sse_response(request, queue, done, cors_origin)

        try:
            await write_sse_stream(resp, queue, done)
        finally:
            conn.sse_queues.pop(session_id, None)
            done.set()

        return resp

    async def _handle_delete(request: web.Request) -> web.Response:
        """Handle DELETE /acp — terminate the connection."""
        conn, source = _resolve_connection(store, request)
        if conn is None:
            return _not_found("Unknown Acp-Connection-Id")

        logger.info("DELETE /acp conn=%s", conn.connection_id)
        await store.remove(conn.connection_id)

        headers = {}
        if cors_origin:
            headers["Access-Control-Allow-Origin"] = cors_origin
        return web.Response(status=202, headers=headers)

    async def _handle_options(request: web.Request) -> web.Response:
        """Handle CORS preflight requests."""
        if not cors_origin:
            return web.Response(status=405)
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": cors_origin,
                "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": (
                    f"{HEADER_CONNECTION_ID}, {HEADER_SESSION_ID}, "
                    "Content-Type, Accept, Authorization"
                ),
                "Access-Control-Max-Age": "86400",
            },
        )

    # ── Periodic cleanup ────────────────────────────────────────────

    async def _cleanup_task(app: web.Application) -> None:
        """Background task that cleans up expired connections."""
        try:
            while True:
                await asyncio.sleep(60)
                await store.cleanup_expired()
                await openai_pool.cleanup_expired()
        except asyncio.CancelledError:
            pass

    # ── Build app ───────────────────────────────────────────────────

    app = web.Application()
    app["store"] = store
    app["cmd"] = cmd
    app["openai_pool"] = openai_pool

    async def _start_cleanup(app: web.Application) -> None:
        asyncio.create_task(_cleanup_task(app))

    app.on_startup.append(_start_cleanup)

    # CORS handling
    if cors_origin:
        app.router.add_route("OPTIONS", "/acp", _handle_options)

    app.router.add_post("/acp", _handle_post)
    app.router.add_get("/acp", _handle_get)
    app.router.add_delete("/acp", _handle_delete)

    # OpenAI compatibility endpoint (optional)
    if openai_handler is not None:
        app.router.add_post("/v1/chat/completions", openai_handler)
        if cors_origin:
            app.router.add_route("OPTIONS", "/v1/chat/completions", _handle_options)

    # Health check
    async def _health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "connections": store.count,
            }
        )

    app.router.add_get("/health", _health)

    return app


async def run_server(
    cmd: list[str],
    *,
    host: str = "0.0.0.0",
    port: int = 8766,
    auth_validator: AuthValidator | None = None,
    max_capacity: int = 50,
    idle_timeout: float = 300.0,
    cors_origin: str | None = None,
    env: dict[str, str] | None = None,
    enable_openai: bool = False,
    openai_pool_max: int = 20,
    openai_pool_idle: float = 600.0,
) -> None:
    """Run the ACP HTTP gateway server.

    Args:
        cmd: The agent command as a list (e.g.
            ``["npx", "pi-acp"]``).
        host: Bind address.
        port: Bind port.
        auth_validator: Auth validator instance.
        max_capacity: Max concurrent connections.
        idle_timeout: Connection idle timeout in seconds.
        cors_origin: CORS origin for browser clients.
        env: Environment variables for the agent subprocess.
        enable_openai: Expose the OpenAI-compatible endpoint.
        openai_pool_max: Max pooled OpenAI sessions.
        openai_pool_idle: Idle timeout for pooled OpenAI sessions.
    """
    app = create_app(
        cmd,
        auth_validator=auth_validator,
        max_capacity=max_capacity,
        idle_timeout=idle_timeout,
        cors_origin=cors_origin,
        env=env,
        enable_openai=enable_openai,
        openai_pool_max=openai_pool_max,
        openai_pool_idle=openai_pool_idle,
    )

    logger.info(
        "ACP HTTP gateway starting on http://%s:%d (cmd=%s, max_conns=%d, openai=%s)",
        host,
        port,
        " ".join(cmd),
        max_capacity,
        enable_openai,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("ACP HTTP gateway listening on http://%s:%d", host, port)

    # Run forever
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("ACP HTTP gateway shutting down")
    finally:
        pool: SessionPool = app["openai_pool"]
        await pool.evict_all()
        await runner.cleanup()
