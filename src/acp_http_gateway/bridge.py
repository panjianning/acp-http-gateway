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
"""Agent subprocess bridge.

Spawns and manages the stdio ACP agent subprocess.  Translates between
HTTP (POST/SSE) and subprocess stdin/stdout.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess as aio_subprocess
import json
import logging
import os
from typing import Any

from .connection import Connection

logger = logging.getLogger(__name__)

BUFFER_SIZE = 50 * 1024 * 1024  # 50 MB


async def spawn_agent(
    cmd: list[str],
    env: dict[str, str] | None = None,
    *,
    auth_context: dict[str, Any] | None = None,
) -> Connection:
    """Spawn a stdio ACP agent subprocess.

    Args:
        cmd: Command and arguments as a list (e.g.
            ``["npx", "pi-acp"]``).
        env: Environment variables for the subprocess.  If ``None``,
            inherits from the current process.
        auth_context: Auth metadata to attach to the connection.

    Returns:
        A :class:`Connection` with the running subprocess.

    Raises:
        RuntimeError: If the subprocess fails to start or has no
            stdin/stdout pipes.
    """
    # Inherit from os.environ and ensure PI_CODING_AGENT_DIR points
    # to the user's default pi agent directory, so pi reads models.json,
    # extensions, etc.  Keeps any caller-supplied env on top.
    if env is None:
        env = dict(os.environ)
    else:
        merged = dict(os.environ)
        merged.update(env)
        env = merged
    env["PI_CODING_AGENT_DIR"] = os.path.expanduser("~/.pi/agent")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=aio_subprocess.PIPE,
        stdout=aio_subprocess.PIPE,
        stderr=aio_subprocess.PIPE,
        env=env,
        limit=BUFFER_SIZE,
    )

    if proc.stdin is None or proc.stdout is None:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Agent subprocess has no stdin/stdout pipes (cmd={cmd})")

    logger.info("Agent spawned [pid=%d] cmd=%s", proc.pid, cmd)

    # Background stderr logging
    async def _log_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "agent [pid=%d] stderr: %s",
                    proc.pid,
                    line.decode(errors="replace").rstrip(),
                )
        except Exception:
            pass

    stderr_task = asyncio.create_task(_log_stderr())

    return Connection(
        proc=proc,
        stdin=proc.stdin,
        stderr_task=stderr_task,
        auth_context=auth_context or {},
    )


async def initialize_agent(
    conn: Connection,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Perform the ACP initialize handshake with the agent subprocess.

    Writes the ``initialize`` JSON-RPC message to stdin, reads the
    response from stdout, and patches the response to include the
    gateway-assigned ``connectionId``.

    Args:
        conn: The connection (agent must already be spawned).
        body: The JSON-RPC initialize request body.

    Returns:
        The JSON-RPC response dict, with ``result.connectionId`` set
        to the gateway's ``Acp-Connection-Id``.

    Raises:
        RuntimeError: If the agent subprocess stdout closes during
            the handshake.
    """
    # Write initialize to agent stdin
    payload = (json.dumps(body, ensure_ascii=False) + "\n").encode()
    conn.stdin.write(payload)
    await conn.stdin.drain()
    logger.debug("→ agent [pid=%d] initialize", conn.proc.pid)

    # Read response from agent stdout (synchronous — we need this before
    # returning the HTTP response to the client)
    try:
        line = await asyncio.wait_for(conn.proc.stdout.readline(), timeout=10.0)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Agent [pid={conn.proc.pid}] did not respond to initialize within 10s"
        )

    if not line:
        raise RuntimeError(
            f"Agent [pid={conn.proc.pid}] stdout closed during initialize"
        )

    try:
        response = json.loads(line.decode(errors="replace").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Agent [pid={conn.proc.pid}] returned invalid JSON: {exc}")

    # Patch the connectionId — the gateway owns connection identity,
    # not the agent.  The agent's stdout goes through us anyway.
    result = response.get("result", {})
    if isinstance(result, dict):
        result["connectionId"] = conn.connection_id

    logger.debug("← agent [pid=%d] initialized: %s", conn.proc.pid, response)
    return response


async def start_stdout_router(conn: Connection) -> asyncio.Task[None]:
    """Start a background task that routes agent stdout to SSE queues.

    Reads line-delimited JSON from the agent's stdout and pushes each
    message to the appropriate SSE queue:

    - Messages with a ``sessionId`` in ``params`` → session-scoped queue.
    - All other messages → connection-scoped queue (``None`` key).

    The task runs until the agent's stdout closes.

    Args:
        conn: The connection whose stdout to route.

    Returns:
        An ``asyncio.Task`` that performs the routing.
    """

    async def _route() -> None:
        try:
            while True:
                line = await conn.proc.stdout.readline()
                if not line:
                    logger.info("agent [pid=%d] stdout closed", conn.proc.pid)
                    break

                text = line.decode(errors="replace").strip()
                if not text:
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "agent [pid=%d] non-JSON stdout: %s", conn.proc.pid, text[:200]
                    )
                    continue

                # Determine target queue
                session_id: str | None = None
                params = msg.get("params", {})
                if isinstance(params, dict):
                    session_id = params.get("sessionId")

                queue = conn.sse_queues.get(session_id)
                if queue is None:
                    # Fall back to connection-scoped queue
                    queue = conn.sse_queues.get(None)

                if queue is not None:
                    await queue.put(msg)
                    logger.debug(
                        "routed→SSE [%s]: %s", session_id or "conn", text[:200]
                    )
                else:
                    logger.debug(
                        "dropped agent stdout (no SSE listener): %s", text[:200]
                    )

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("stdout router error [pid=%d]", conn.proc.pid)
        finally:
            # Signal all SSE streams that no more messages are coming
            for key in list(conn.sse_queues):
                q = conn.sse_queues.pop(key, None)
                if q is not None:
                    # Push a sentinel to notify listeners
                    await q.put({"__sentinel__": True})

    task_name = f"stdout-router-{conn.connection_id}"
    return asyncio.create_task(_route(), name=task_name)


async def write_to_agent(conn: Connection, body: dict[str, Any]) -> None:
    """Write a JSON-RPC message to the agent's stdin.

    Args:
        conn: The target connection.
        body: The JSON-RPC request body.
    """
    payload = (json.dumps(body, ensure_ascii=False) + "\n").encode()
    conn.stdin.write(payload)
    await conn.stdin.drain()
    method = body.get("method", "?")
    logger.debug("→ agent [pid=%d] %s", conn.proc.pid, method)
