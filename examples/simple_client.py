#!/usr/bin/env python3
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
"""Minimal Python client for the ACP Streamable HTTP Gateway.

Demonstrates:
1. ``initialize`` via POST (with optional bearer token)
2. Opening the SSE stream via GET
3. ``session/new`` → ``session/prompt``
4. Consuming SSE events (tool calls, messages, permission requests)

Usage::

    # Start the gateway first:
    #   uv run acp-http-gateway --cmd "npx pi-acp" --cors-origin "*"

    # Then run this client:
    uv run python examples/simple_client.py \\
        --base-url http://localhost:8766 \\
        --prompt "你好，请介绍一下自己"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urljoin

import aiohttp

HEADER_CONNECTION_ID = "Acp-Connection-Id"
HEADER_SESSION_ID = "Acp-Session-Id"


def _json_rpc(method: str, msg_id: int, params: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "id": msg_id,
        "params": params or {},
    }


class AcpHttpClient:
    """ACP Streamable HTTP client."""

    def __init__(self, base_url: str, bearer_token: str | None = None) -> None:
        self._base_url: str = base_url.rstrip("/")
        self._bearer_token: str | None = bearer_token
        self._connection_id: str | None = None
        self._session_id: str | None = None
        self._msg_id: int = 0
        self._sse_task: asyncio.Task | None = None
        self._sse_queue: asyncio.Queue[dict] | None = None
        self._http: aiohttp.ClientSession | None = None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _headers(
        self,
        session_scoped: bool = False,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build HTTP headers for a request.

        Never includes ``Content-Type`` — aiohttp sets it automatically
        when ``json=...`` is passed.
        """
        h: dict[str, str] = {}
        if self._bearer_token:
            h["Authorization"] = f"Bearer {self._bearer_token}"
        if self._connection_id:
            h[HEADER_CONNECTION_ID] = self._connection_id
        if self._session_id and session_scoped:
            h[HEADER_SESSION_ID] = self._session_id
        if extra:
            h.update(extra)
        return h

    async def connect(self) -> None:
        """Initialize the connection and open the SSE stream."""
        if self._http is not None:
            return
        self._http = aiohttp.ClientSession()

        # ── Step 1: initialize ──
        msg = _json_rpc(
            "initialize",
            self._next_id(),
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "simple-client", "version": "0.1.0"},
            },
        )
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"initialize failed [{resp.status}]: {body}")
            self._connection_id = resp.headers.get(HEADER_CONNECTION_ID)
            result = await resp.json()
            print(f"[initialize] connection_id={self._connection_id}")
            print(f"[initialize] capabilities={json.dumps(result, indent=2)}")

        # ── Step 2: open SSE stream ──
        self._sse_queue = asyncio.Queue()
        self._sse_task = asyncio.create_task(self._read_sse(), name="sse-reader")

    async def _read_sse(self) -> None:
        """Background task: read SSE events into the queue."""
        headers = self._headers(extra={"Accept": "text/event-stream"})
        async with self._http.get(
            urljoin(self._base_url, "/acp"),
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"SSE stream failed [{resp.status}]: {body}")
            buffer = ""
            async for chunk in resp.content.iter_any():
                text = chunk.decode(errors="replace")
                buffer += text
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    data = None
                    for line in event_str.split("\n"):
                        if line.startswith("data: "):
                            data = line[6:]
                    if data:
                        try:
                            msg = json.loads(data)
                            self._sse_queue.put_nowait(msg)
                        except json.JSONDecodeError:
                            pass

    async def new_session(self) -> str:
        """Create a new ACP session."""
        req_id = self._next_id()
        msg = _json_rpc(
            "session/new",
            req_id,
            {"cwd": "/tmp", "mcpServers": []},
        )
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(),
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise RuntimeError(f"session/new failed [{resp.status}]: {body}")

        session_id = await self._wait_for_response(req_id, "session/new")
        print(f"[session/new] session_id={session_id}")
        self._session_id = session_id
        return session_id

    async def _wait_for_response(self, req_id: int, label: str = "?") -> str:
        """Wait on the SSE queue for a response matching *req_id*."""
        while True:
            event = await asyncio.wait_for(self._sse_queue.get(), timeout=30)
            if event.get("id") == req_id:
                if "error" in event:
                    raise RuntimeError(f"{label} error: {event['error']['message']}")
                return event.get("result", {}).get("sessionId", "")

    async def list_sessions(self) -> None:
        """List ACP sessions."""
        req_id = self._next_id()
        msg = _json_rpc("session/list", req_id)
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(),
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise RuntimeError(f"session/list failed [{resp.status}]: {body}")
        await self._wait_for_response(req_id, "session/list")

    async def load_session(self, session_id: str, cwd: str) -> str:
        """Load an existing ACP session."""
        req_id = self._next_id()
        msg = _json_rpc(
            "session/load",
            req_id,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
        )
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(),
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise RuntimeError(f"session/load failed [{resp.status}]: {body}")

        await self._wait_for_response(req_id, "session/load")
        self._session_id = session_id
        print(f"[session/load] loaded session_id={session_id}")
        return session_id

    async def prompt(self, text: str) -> None:
        """Send a prompt and stream the response."""
        msg = _json_rpc(
            "session/prompt",
            self._next_id(),
            {"sessionId": self._session_id, "prompt": [{"type": "text", "text": text}]},
        )
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(session_scoped=True),
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise RuntimeError(f"session/prompt failed [{resp.status}]: {body}")

        print(f"[prompt] → {text}")

        prompt_id = msg["id"]
        while True:
            try:
                event = await asyncio.wait_for(self._sse_queue.get(), timeout=120)
            except asyncio.TimeoutError:
                print("[prompt] timed out waiting for response")
                break

            if "__sentinel__" in event:
                break

            if event.get("id") == prompt_id:
                result = event.get("result", {})
                stop_reason = result.get("stopReason", "?")
                print(f"[prompt] ← done (stopReason={stop_reason})")
                break

            method = event.get("method", "")
            if method == "request_permission":
                params = event.get("params", {})
                tool = params.get("toolCall", {})
                print(f"[prompt] ⚠️  permission requested: {tool.get('title', '?')}")
                approvemsg = {
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "result": {"outcome": "allow_once"},
                }
                async with self._http.post(
                    urljoin(self._base_url, "/acp"),
                    json=approvemsg,
                    headers=self._headers(session_scoped=True),
                ) as r:
                    if r.status == 202:
                        print("[prompt] → approved")
            else:
                params = event.get("params", {})
                update = params.get("sessionUpdate", "")
                if "message" in params:
                    content = params["message"].get("content", "")
                    if isinstance(content, str):
                        print(f"[prompt] ← {content}")
                elif update:
                    pass  # suppress detail for readability

    async def cancel(self) -> None:
        """Cancel the current turn."""
        msg = _json_rpc(
            "session/cancel",
            0,  # notification — no response expected
            {"sessionId": self._session_id},
        )
        async with self._http.post(
            urljoin(self._base_url, "/acp"),
            json=msg,
            headers=self._headers(session_scoped=True),
        ) as resp:
            print(f"[cancel] status={resp.status}")

    async def close(self) -> None:
        """Terminate the connection."""
        if self._connection_id and self._http:
            async with self._http.delete(
                urljoin(self._base_url, "/acp"),
                headers=self._headers(),
            ) as resp:
                print(f"[close] status={resp.status}")
        if self._sse_task:
            self._sse_task.cancel()
        if self._http:
            await self._http.close()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="ACP HTTP Gateway test client",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8766",
        help="Gateway base URL (default: http://localhost:8766)",
    )
    parser.add_argument(
        "--bearer-token",
        default=None,
        help="Bearer token for authentication",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="User prompt text",
    )
    args = parser.parse_args()

    client = AcpHttpClient(args.base_url, args.bearer_token)

    try:
        await client.connect()
        await client.new_session()
        await client.prompt(args.prompt)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
