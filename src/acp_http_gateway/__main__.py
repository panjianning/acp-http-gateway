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
"""CLI entry point for acp-http-gateway.

Usage::

    acp-http-gateway --cmd "npx pi-acp"

    # With bearer token auth
    acp-http-gateway --cmd "npx -y @agentclientprotocol/codex-acp" \\
                     --bearer-token sk-xxx

    # Custom port, CORS for local dev
    acp-http-gateway --cmd "..." --port 8080 --cors-origin "*"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import sys

from .auth import BearerTokenValidator, NoAuthValidator
from .server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ACP Streamable HTTP Gateway — bridges HTTP to stdio ACP agents",
    )

    # ── Agent command ───────────────────────────────────────────────
    parser.add_argument(
        "--cmd",
        required=True,
        help="ACP agent command (e.g. 'npx pi-acp')",
    )

    # ── Server configuration ───────────────────────────────────────
    parser.add_argument(
        "--host",
        default=os.environ.get("ACP_HTTP_HOST", "0.0.0.0"),
        help="Bind address (env: ACP_HTTP_HOST, default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ACP_HTTP_PORT", "8766")),
        help="Bind port (env: ACP_HTTP_PORT, default: 8766)",
    )
    parser.add_argument(
        "--max-capacity",
        type=int,
        default=int(os.environ.get("ACP_MAX_CAPACITY", "50")),
        help="Max concurrent connections (env: ACP_MAX_CAPACITY, default: 50)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("ACP_IDLE_TIMEOUT", "300")),
        help="Connection idle timeout in seconds (env: ACP_IDLE_TIMEOUT, default: 300)",
    )

    # ── CORS ────────────────────────────────────────────────────────
    parser.add_argument(
        "--cors-origin",
        default=os.environ.get("ACP_CORS_ORIGIN"),
        help="CORS Access-Control-Allow-Origin value (env: ACP_CORS_ORIGIN)",
    )

    # ── Authentication ──────────────────────────────────────────────
    parser.add_argument(
        "--bearer-token",
        default=os.environ.get("ACP_BEARER_TOKEN"),
        help="Require Bearer token authentication (env: ACP_BEARER_TOKEN)",
    )

    # ── OpenAI compatibility layer ────────────────────────────────
    parser.add_argument(
        "--enable-openai",
        action="store_true",
        default=os.environ.get("ACP_ENABLE_OPENAI", "") == "1",
        help="Expose POST /v1/chat/completions (OpenAI-compatible) "
        "(env: ACP_ENABLE_OPENAI=1)",
    )
    parser.add_argument(
        "--openai-pool-max",
        type=int,
        default=int(os.environ.get("ACP_OPENAI_POOL_MAX", "20")),
        help="Max pooled OpenAI sessions (env: ACP_OPENAI_POOL_MAX, default: 20)",
    )
    parser.add_argument(
        "--openai-pool-idle",
        type=float,
        default=float(os.environ.get("ACP_OPENAI_POOL_IDLE", "600")),
        help="OpenAI session idle timeout seconds "
        "(env: ACP_OPENAI_POOL_IDLE, default: 600)",
    )

    # ── Logging ─────────────────────────────────────────────────────
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ACP_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (env: ACP_LOG_LEVEL, default: INFO)",
    )

    args = parser.parse_args()

    # Parse command
    cmd = shlex.split(args.cmd)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Configure auth
    auth_validator = (
        BearerTokenValidator(args.bearer_token)
        if args.bearer_token
        else NoAuthValidator()
    )

    # Run
    try:
        asyncio.run(
            run_server(
                cmd=cmd,
                host=args.host,
                port=args.port,
                auth_validator=auth_validator,
                max_capacity=args.max_capacity,
                idle_timeout=args.idle_timeout,
                cors_origin=args.cors_origin,
                enable_openai=args.enable_openai,
                openai_pool_max=args.openai_pool_max,
                openai_pool_idle=args.openai_pool_idle,
            )
        )
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        logging.getLogger(__name__).info("Shutdown by signal")


if __name__ == "__main__":
    main()
