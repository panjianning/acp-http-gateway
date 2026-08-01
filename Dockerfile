# syntax=docker/dockerfile:1
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

# ─────────────────────────────────────────────────────────────────────
# acp-http-gateway — all-in-one image
#
# Everything is baked in:
#   - acp-http-gateway  (Python, cloned from GitHub)
#   - pi                (Node agent, npm global)
#   - pi-acp            (Node ACP adapter, npm global)
#
# Build (no local repo needed):
#   docker build -t acp-http-gateway:latest .
#
# Run:
#   docker run --rm -p 8766:8766 \
#       -e ACP_BEARER_TOKEN=sk-xxx \
#       -v ~/.pi/agent:/root/.pi/agent \
#       acp-http-gateway:latest
#
# The only host dependency is the pi config directory (~/.pi/agent),
# mounted as a volume.  It holds models.json (providers + API keys),
# sessions/, skills/ and extensions/.
# ─────────────────────────────────────────────────────────────────────

# ── Stage 1: Node runtime (node + npm only) ────────────────────────
FROM node:22-slim AS node-runtime

# ── Stage 2: runtime (python + uv + gateway + node + pi) ───────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Copy the Node runtime (binary + npm, including npm's own deps).
# node:22-slim and bookworm-slim are both Debian bookworm, so the
# dynamically-linked node binary runs fine.
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/
COPY --from=node-runtime /usr/local/bin/npm /usr/local/bin/
COPY --from=node-runtime /usr/local/bin/npx /usr/local/bin/
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm/

# Install pi + pi-acp in THIS image so npm resolves the full dependency
# tree (incl. @agentclientprotocol/sdk) against the actual runtime fs.
# Probe: run an initialize handshake during build — fails fast if the
# SDK dependency is missing.
RUN npm install -g @mariozechner/pi-coding-agent pi-acp \
    && echo '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
       | pi-acp | head -1 \
       | grep -q '"jsonrpc":"2.0"'

# Git + clone the gateway from GitHub (no local build context needed)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 https://github.com/panjianning/acp-http-gateway.git
WORKDIR /opt/acp-http-gateway
RUN uv sync --no-dev

# pi configuration directory — mount the host's ~/.pi/agent here.
ENV PI_CODING_AGENT_DIR=/root/.pi/agent
RUN mkdir -p /root/.pi/agent
VOLUME ["/root/.pi/agent"]

# Headless: suppress extension TUI notifications.
ENV PI_EXT_QUIET=1

EXPOSE 8766

# ACP_ENABLE_OPENAI / ACP_BEARER_TOKEN are read by the CLI at runtime.
ENV ACP_ENABLE_OPENAI=1

ENTRYPOINT [".venv/bin/acp-http-gateway"]
CMD ["--cmd", "pi-acp", "--host", "0.0.0.0", "--port", "8766"]
