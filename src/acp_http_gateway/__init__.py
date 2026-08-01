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
"""Streamable HTTP Gateway for the Agent Client Protocol.

Provides an HTTP endpoint (``/acp``) that bridges ACP JSON-RPC messages
between HTTP clients (browsers, scripts, SDKs) and stdio-based ACP agents.

The gateway implements the `ACP Streamable HTTP & WebSocket Transport`_
specification, which adds HTTP-based transport to ACP alongside the
existing stdio and WebSocket transports.

Architecture::

    Browser / Client ── HTTP (POST/GET/DELETE) ──► /acp
                           │
                    ┌──────▼──────────────────────────┐
                    │  acp-http-gateway               │
                    │                                  │
                    │  POST /acp  → agent stdin       │
                    │  GET  /acp  ← agent stdout (SSE)│
                    │  DELETE /acp → kill agent       │
                    └──────┬──────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │  ACP Agent  │  (stdio subprocess)
                    │  (codex,    │
                    │   pi,│
                    │   custom)   │
                    └─────────────┘

Usage::

    # Start the gateway with a stdio ACP agent
    acp-http-gateway --cmd "npx -y @agentclientprotocol/codex-acp" --port 8766

    # With bearer token authentication
    acp-http-gateway --cmd "npx pi-acp" \\
                     --bearer-token "sk-xxx"

    # From Python
    from acp_http_gateway import run_server
    import asyncio
    asyncio.run(run_server(
        cmd=["npx", "pi-acp"],
        port=8766,
    ))

.. _ACP Streamable HTTP & WebSocket Transport:
    https://agentclientprotocol.com/rfds/streamable-http-websocket-transport
"""

from .server import run_server, create_app
from .auth import AuthValidator, BearerTokenValidator, NoAuthValidator
from .openai import SessionPool, PooledSession, make_openai_handler

__all__ = [
    "run_server",
    "create_app",
    "AuthValidator",
    "BearerTokenValidator",
    "NoAuthValidator",
    "SessionPool",
    "PooledSession",
    "make_openai_handler",
]
__version__ = "0.2.0"
