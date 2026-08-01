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
"""OpenAI Chat Completions handler.

Implements ``POST /v1/chat/completions`` on top of the ACP gateway's
existing connection/bridge infrastructure.  Sessions are pooled by
``X-ACP-Session-Id`` header for stateful multi-turn conversations.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from ..bridge import initialize_agent, spawn_agent, start_stdout_router, write_to_agent
from ..connection import Connection
from .openai_format import (
    build_prompt,
    json_dumps,
    make_chat_id,
    make_chunk,
    make_error,
    make_response,
    parse_chat_request,
)
from .session_pool import PooledSession, SessionPool

logger = logging.getLogger(__name__)

HEADER_SESSION_ID = "X-ACP-Session-Id"
PROMPT_TIMEOUT = 300.0


class ModelNotFoundError(RuntimeError):
    """Raised when the requested model cannot be set on the agent."""


def _json_dumps(data: Any) -> str:
    """Serialize JSON without escaping non-ASCII characters (UTF-8)."""
    return json.dumps(data, ensure_ascii=False)


async def _spawn_pooled_connection(
    cmd: list[str],
    env: dict[str, str] | None,
) -> Connection:
    """Spawn an agent connection and perform the initialize handshake.

    Unlike the public ``/acp`` initialize flow, this connection is not
    registered in the public :class:`ConnectionStore` — it is managed
    exclusively by the :class:`SessionPool`.

    Args:
        cmd: The agent command.
        env: Environment overrides for the subprocess.

    Returns:
        A ready :class:`Connection`.

    Raises:
        RuntimeError: If spawn or handshake fails.
    """
    conn = await spawn_agent(cmd, env=env, auth_context={"openai": True})
    # Register the connection-scoped queue BEFORE any session operation
    # so the stdout router has a target for responses (which carry no
    # ``sessionId`` in params and therefore route to the ``None`` queue).
    conn.sse_queues[None] = asyncio.Queue()

    body = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "acp-http-gateway-openai", "version": "0.1.0"},
        },
    }
    await initialize_agent(conn, body)
    await start_stdout_router(conn)
    return conn


async def _read_conn_queue(
    conn: Connection, req_id: int, timeout: float = 30.0
) -> dict:
    """Read from the connection-scoped queue until the response matches.

    Args:
        conn: The connection.
        req_id: The JSON-RPC id to wait for.
        timeout: Max seconds to wait.

    Returns:
        The matched JSON-RPC message.

    Raises:
        asyncio.TimeoutError: If the response never arrives.
    """
    queue = conn.sse_queues[None]
    while True:
        msg = await asyncio.wait_for(queue.get(), timeout=timeout)
        if msg.get("__sentinel__"):
            raise RuntimeError("Agent stdout closed")
        if msg.get("id") == req_id:
            return msg


async def _create_session(conn: Connection, cwd: str) -> tuple[str, str | None]:
    """Create a new ACP session on a connection.

    Args:
        conn: The connection.
        cwd: Working directory for the session.

    Returns:
        A tuple of ``(session_id, startup_info)`` where *startup_info*
        is the agent's prelude banner (``_meta.piAcp.startupInfo``) or
        ``None``.

    Raises:
        RuntimeError: If the agent returns no sessionId.
    """
    req_id = 2
    body = {
        "jsonrpc": "2.0",
        "method": "session/new",
        "id": req_id,
        "params": {"cwd": cwd, "mcpServers": []},
    }
    await write_to_agent(conn, body)
    msg = await _read_conn_queue(conn, req_id)
    result = msg.get("result", {})
    session_id = result.get("sessionId", "")
    if not session_id:
        raise RuntimeError("session/new returned no sessionId")
    startup_info: str | None = None
    meta = result.get("_meta", {})
    if isinstance(meta, dict):
        pi_acp = meta.get("piAcp", {})
        if isinstance(pi_acp, dict):
            startup_info = pi_acp.get("startupInfo")
    return session_id, startup_info


async def _set_model(conn: Connection, acp_session_id: str, model: str) -> None:
    """Set the agent model via ``session/set_config_option``.

    Uses the standard ACP config option with the ``model`` id
    (pi-acp's ``MODEL_CONFIG_ID``), which routes to pi's ``set_model``
    RPC.  (``session/set_model`` is NOT a registered ACP method in
    pi-acp.)

    Args:
        conn: The connection.
        acp_session_id: The ACP session id.
        model: The model id (e.g. ``huya/deepseek/deepseek-v4-pro``).

    Raises:
        ModelNotFoundError: If the agent rejects the model id.
        RuntimeError: If the agent does not respond in time.
    """
    req_id = 4
    body = {
        "jsonrpc": "2.0",
        "method": "session/set_config_option",
        "id": req_id,
        "params": {
            "sessionId": acp_session_id,
            "configId": "model",
            "value": model,
        },
    }
    await write_to_agent(conn, body)
    try:
        msg = await _read_conn_queue(conn, req_id, timeout=10.0)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Agent did not respond to set_config_option in time"
        ) from exc

    if "error" in msg:
        err = msg.get("error", {})
        detail = (
            err.get("message", "unknown error") if isinstance(err, dict) else str(err)
        )
        raise ModelNotFoundError(f"model '{model}' rejected by agent: {detail}")


async def _prompt(
    conn: Connection,
    acp_session_id: str,
    text: str,
    model: str,
    startup_info: str | None = None,
) -> dict[str, Any]:
    """Send a prompt to the agent and collect the full assistant reply.

    Reads from **both** queues: ``session/update`` notifications arrive
    on the session-scoped queue (``conn.sse_queues[acp_session_id]``)
    while the JSON-RPC response arrives on the connection-scoped queue
    (``conn.sse_queues[None]``).

    Args:
        conn: The connection.
        acp_session_id: The ACP session id.
        text: The user prompt text.
        model: Model id; switches the agent via ``session/set_config_option``.
        startup_info: If set, the agent's prelude banner text.  The first
            ``agent_message_chunk`` matching it is dropped (pi-acp emits
            its startup banner as the first agent message of a new
            session).

    Returns:
        ``{"content": str, "stop_reason": str}``.
    """
    # Best-effort model switch before the prompt.
    if model:
        await _set_model(conn, acp_session_id, model)

    # Register the session-scoped queue for this turn.
    session_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    conn.sse_queues[acp_session_id] = session_queue
    conn_queue = conn.sse_queues[None]

    req_id = 3
    body = {
        "jsonrpc": "2.0",
        "method": "session/prompt",
        "id": req_id,
        "params": {
            "sessionId": acp_session_id,
            "prompt": [{"type": "text", "text": text}],
        },
    }
    await write_to_agent(conn, body)

    chunks: list[str] = []
    stop_reason = "stop"
    banner_seen = ""  # accumulated text of dropped startup-banner chunks

    # Maintain one pending ``get()`` task per queue.  On each iteration
    # replenish consumed queues, wait for the first completion, then
    # **drain** both queues of all available items.  Draining matters
    # because the router may have queued multiple updates (and the
    # response) before our reader wakes up — reading one item per queue
    # per iteration would drop updates that arrived in the same batch.
    readers: dict[asyncio.Queue[dict[str, Any]], asyncio.Task[dict[str, Any]]] = {}

    try:
        while True:
            # Replenish readers for consumed queues.
            for q in (conn_queue, session_queue):
                if q not in readers or readers[q].done():
                    readers[q] = asyncio.create_task(q.get())

            done, _pending = await asyncio.wait(
                readers.values(),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=PROMPT_TIMEOUT,
            )
            if not done:
                raise RuntimeError("Timed out waiting for agent response")

            # Collect items from BOTH sources:
            #  1. completed reader tasks (their .result() IS the message)
            #  2. drain remaining queued items via get_nowait()
            items: list[dict[str, Any]] = [t.result() for t in done]
            for q in (conn_queue, session_queue):
                while True:
                    try:
                        items.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break

            response_msg: dict[str, Any] | None = None
            for msg in items:
                if msg.get("__sentinel__"):
                    raise RuntimeError("Agent stdout closed")
                if msg.get("id") == req_id:
                    response_msg = msg
                    continue
                # Otherwise it's a session/update notification.
                params = msg.get("params", {})
                update = params.get("update", {})
                su = update.get("sessionUpdate", "")
                content = update.get("content", {})
                if su == "agent_message_chunk" and isinstance(content, dict):
                    piece = content.get("text", "")
                    if piece:
                        # Drop the agent's startup banner.  pi-acp may
                        # emit the prelude in several chunks, so drop
                        # leading chunks that together form a prefix of
                        # the known startup_info text.  Once a chunk no
                        # longer extends that prefix, the real reply has
                        # begun and we keep everything from there.
                        if startup_info and startup_info.startswith(
                            banner_seen + piece
                        ):
                            banner_seen += piece
                            continue
                        chunks.append(piece)

            if response_msg is not None:
                result = response_msg.get("result", {})
                stop_reason = result.get("stopReason", "stop")
                return {"content": "".join(chunks), "stop_reason": stop_reason}
    finally:
        for task in readers.values():
            task.cancel()


def make_openai_handler(
    cmd: list[str],
    env: dict[str, str] | None,
    pool: SessionPool,
) -> Any:
    """Create the ``POST /v1/chat/completions`` handler.

    Args:
        cmd: The agent command.
        env: Environment overrides for the subprocess.
        pool: The session pool to use.

    Returns:
        An aiohttp request handler coroutine.
    """

    async def _handle(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                make_error("Invalid JSON body", "invalid_request"),
                status=400,
                dumps=_json_dumps,
            )

        try:
            req = parse_chat_request(body)
        except ValueError as exc:
            return web.json_response(
                make_error(str(exc), "invalid_request"),
                status=400,
                dumps=_json_dumps,
            )

        messages = req["messages"]
        model = req["model"]
        stream = req["stream"]
        session_id = req["session_id"]
        cwd = req["cwd"]

        pooled = pool.get(session_id) if session_id else None
        if pooled is None:
            try:
                conn = await _spawn_pooled_connection(cmd, env)
                acp_session_id, startup_info = await _create_session(conn, cwd)
                pooled = PooledSession(conn, acp_session_id, startup_info)
                if not session_id:
                    session_id = acp_session_id
                pool.put(session_id, pooled)
            except Exception as exc:
                logger.exception("Failed to create session")
                return web.json_response(
                    make_error(f"Failed to create session: {exc}"),
                    status=502,
                    dumps=_json_dumps,
                )

        try:
            text = build_prompt(messages)
            if not text:
                return web.json_response(
                    make_error("No user message found", "invalid_request"),
                    status=400,
                    dumps=_json_dumps,
                )

            result = await _prompt(
                pooled.connection,
                pooled.acp_session_id,
                text,
                model,
                startup_info=pooled.startup_info,
            )
            pooled.touch()

            chat_id = make_chat_id()

            if stream:
                return await _stream_response(
                    request, chat_id, model, result, session_id
                )

            resp = make_response(
                chat_id,
                model,
                result["content"],
                finish_reason=_map_stop(result["stop_reason"]),
            )
            headers = {HEADER_SESSION_ID: session_id}
            return web.json_response(resp, headers=headers, dumps=_json_dumps)
        except ModelNotFoundError as exc:
            # OpenAI returns 404 for unknown models.
            return web.json_response(
                make_error(str(exc), "model_not_found"),
                status=404,
                dumps=_json_dumps,
            )
        except Exception as exc:
            logger.exception("Prompt failed")
            return web.json_response(
                make_error(f"Prompt failed: {exc}"),
                status=502,
                dumps=_json_dumps,
            )

    return _handle


def _map_stop(stop_reason: str) -> str:
    """Map ACP stop reason to OpenAI finish_reason."""
    if stop_reason in ("end_turn", "stop"):
        return "stop"
    if stop_reason in ("max_tokens", "max_length"):
        return "length"
    return stop_reason or "stop"


async def _stream_response(
    request: web.Request,
    chat_id: str,
    model: str,
    result: dict[str, Any],
    session_id: str,
) -> web.StreamResponse:
    """Write an OpenAI streaming response as SSE.

    Args:
        request: The incoming request.
        chat_id: The completion id.
        model: The model name.
        result: ``{"content": str, "stop_reason": str}``.
        session_id: The session id (sent in the response header).

    Returns:
        A streamed ``text/event-stream`` response.
    """
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            HEADER_SESSION_ID: session_id,
        },
    )
    await resp.prepare(request)

    role_chunk = make_chunk(chat_id, model, {"role": "assistant"}, None)
    await resp.write(f"data: {json_dumps(role_chunk)}\n\n".encode())

    for piece in _split_text(result["content"]):
        delta_chunk = make_chunk(chat_id, model, {"content": piece}, None)
        await resp.write(f"data: {json_dumps(delta_chunk)}\n\n".encode())

    final_chunk = make_chunk(chat_id, model, {}, _map_stop(result["stop_reason"]))
    await resp.write(f"data: {json_dumps(final_chunk)}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


def _split_text(text: str, size: int = 64) -> list[str]:
    """Split text into chunks for streaming.

    Args:
        text: The full text.
        size: Maximum chunk size in characters.

    Returns:
        A list of text pieces.
    """
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]
