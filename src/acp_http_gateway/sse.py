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
"""Server-Sent Events (SSE) formatting utilities.

SSE is used for the ``GET /acp`` endpoint — all server→client messages
(JSON-RPC responses and notifications) are delivered as SSE events over
a long-lived HTTP connection.

Format (per the `SSE spec`_)::

    event: message
    data: {"jsonrpc":"2.0",...}

    : heartbeat

.. _SSE spec: https://html.spec.whatwg.org/multipage/server-sent-events.html
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from aiohttp import web

logger = logging.getLogger(__name__)

# How often to send a keepalive comment to prevent proxy timeouts.
HEARTBEAT_INTERVAL = 30.0  # seconds


def _format_sse_event(data: dict[str, Any]) -> str:
    """Format a single SSE event.

    Args:
        data: A JSON-serializable dict to send as the event data.

    Returns:
        A string in the SSE wire format::

            event: message
            data: <json>\n\n
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # SSE data lines can be multi-line; each line is prefixed with "data: "
    lines = "\n".join(f"data: {line}" for line in payload.split("\n"))
    return f"event: message\n{lines}\n\n"


def _format_heartbeat() -> str:
    """Format a keepalive comment.

    SSE comments (lines starting with ``:``) are ignored by clients
    but keep proxies and load balancers from timing out the connection.

    Returns:
        A string like ``": heartbeat\n\n"``.
    """
    return ": heartbeat\n\n"


async def sse_generator(
    queue: asyncio.Queue[dict[str, Any]],
    done: asyncio.Event,
) -> AsyncIterator[str]:
    """Async generator that yields SSE events from a queue.

    Pops messages from *queue* and formats them as SSE events.
    Sends periodic heartbeat comments to keep the connection alive.
    Exits when *done* is set and the queue is drained.

    Args:
        queue: Message queue fed by the agent stdout router.
        done: Set to signal that no more messages will be produced.

    Yields:
        SSE-formatted strings.
    """
    last_heartbeat = time.monotonic()
    drained = False

    while not drained:
        elapsed = time.monotonic() - last_heartbeat
        wait_time = HEARTBEAT_INTERVAL - elapsed

        try:
            msg = await asyncio.wait_for(queue.get(), timeout=max(wait_time, 0.5))
            yield _format_sse_event(msg)
            last_heartbeat = time.monotonic()
        except asyncio.TimeoutError:
            # Send heartbeat
            yield _format_heartbeat()
            last_heartbeat = time.monotonic()
            if done.is_set() and queue.empty():
                drained = True
            continue

        if done.is_set() and queue.empty():
            drained = True

    logger.debug("SSE generator finished")


async def sse_response(
    request: web.Request,
    queue: asyncio.Queue[dict[str, Any]],
    done: asyncio.Event,
    cors_origin: str | None = None,
) -> web.StreamResponse:
    """Create and prepare an SSE :class:`~aiohttp.web.StreamResponse`.

    Args:
        request: The incoming GET request.
        queue: Message queue for this stream.
        done: Signal for termination.
        cors_origin: If set, added as ``Access-Control-Allow-Origin``.

    Returns:
        A prepared :class:`~aiohttp.web.StreamResponse` ready to be used
        as the response body for an SSE endpoint.
    """
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            **({"Access-Control-Allow-Origin": cors_origin} if cors_origin else {}),
        },
    )
    await resp.prepare(request)
    return resp


async def write_sse_stream(
    resp: web.StreamResponse,
    queue: asyncio.Queue[dict[str, Any]],
    done: asyncio.Event,
) -> None:
    """Write SSE events to a prepared response until completion.

    This is the main loop for an SSE GET handler.  It iterates the
    :func:`sse_generator` and writes each formatted event to the response.

    Args:
        resp: A prepared :class:`~aiohttp.web.StreamResponse`.
        queue: Message queue for this stream.
        done: Signal for termination.
    """
    try:
        async for event_text in sse_generator(queue, done):
            await resp.write(event_text.encode("utf-8"))
    except ConnectionResetError:
        logger.debug("SSE client disconnected")
    except Exception:
        logger.exception("SSE stream error")
    finally:
        await resp.write_eof()
