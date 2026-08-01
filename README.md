# acp-http-gateway

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230)](https://docs.astral.sh/ruff/)

Streamable HTTP gateway for the [Agent Client Protocol (ACP)][acp].

Bridges HTTP clients (browsers, scripts, SDKs) to stdio-based ACP agents.
Implements the [Streamable HTTP & WebSocket Transport][spec] specification.

## Why?

Browser WebSocket (`new WebSocket(url)`) **cannot send custom HTTP headers**,
making authentication painful.  This gateway solves that by adding a standard
HTTP-based transport:

- **POST `/acp`** → Send JSON-RPC to the agent (supports `Authorization` header)
- **GET `/acp`** → Open an SSE stream for agent responses
- **DELETE `/acp`** → Terminate the connection

## Install

```bash
git clone https://github.com/example/acp-http-gateway.git
cd acp-http-gateway

# Use Homebrew Python + uv
uv sync --python /opt/homebrew/opt/python@3.12/bin/python3.12
```

## Quick Start (Docker — 推荐)

一键启动，镜像内已包含 **acp-http-gateway + pi + pi-acp**，无需安装任何依赖。
镜像由 GitHub Actions 自动构建（多架构），托管在
`ghcr.io/panjianning/acp-http-gateway`：

```bash
# 1. 准备模型配置（含 API Key）
#    vim ~/.pi/agent/models.json

# 2. 启动（直接 pull，不用自己构建）
docker run -d --name acp-http-gateway \
  -p 8766:8766 \
  -e ACP_BEARER_TOKEN=你的token \
  -v ~/.pi/agent:/root/.pi/agent \
  ghcr.io/panjianning/acp-http-gateway:latest

# 3. 测试
curl http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的token" \
  -d '{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'
```

详见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**。

## Quick Start (源码)

```bash
# Start the gateway with any stdio ACP agent
uv run acp-http-gateway \
  --cmd "npx pi-acp" \
  --cors-origin "*"

# In another terminal — initialize and chat
uv run python examples/simple_client.py \
  --base-url http://localhost:8766 \
  --prompt "你好，请介绍一下自己"
```

### With Authentication

```bash
# Start gateway with bearer token
uv run acp-http-gateway \
  --cmd "npx pi-acp" \
  --bearer-token "sk-secret"

# Client passes the token
uv run python examples/simple_client.py \
  --base-url http://localhost:8766 \
  --bearer-token "sk-secret" \
  --prompt "Hello"
```

### Browser Demo

Open `examples/browser-client.html` in your browser.  Enter the gateway URL
and optional bearer token, then click **Connect**.

### OpenAI-compatible API

Enable the optional OpenAI Chat Completions layer with `--enable-openai`:

```bash
uv run acp-http-gateway --cmd "npx pi-acp" --enable-openai
```

Now `POST /v1/chat/completions` works like a standard OpenAI endpoint,
while the underlying agent keeps its own session state:

```bash
# Non-streaming — returns the assistant's reply
curl -s http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "example/deepseek-v4-pro",
    "messages": [{"role": "user", "content": "你好"}]
  }'

# Streaming — SSE chunks (OpenAI format)
curl -s -N http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "example/deepseek-v4-pro",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

**Stateful sessions:** the response includes an `X-ACP-Session-Id` header.
Pass it back in the request body as `session_id` to continue the same
conversation (the agent remembers prior turns):

```bash
# Turn 1 → grab X-ACP-Session-Id from the response headers
# Turn 2 → reuse it
curl -s http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "接着刚才的话题"}],
    "session_id": "019fbb48-ae22-7360-858c-9e1be5d8db55"
  }'
```

Notes:

- Tool calls are **not** exposed — the agent executes its own toolchain
  and only the final text is returned.
- `usage` is always zero (ACP has no token accounting).
- The agent's startup banner is stripped from the first reply.
- The OpenAI layer uses a session pool (`--openai-pool-max`, default 20;
  `--openai-pool-idle`, default 600s).  Idle sessions are evicted; if a
  client returns with an evicted `session_id`, a new session is created.

See **[docs/http-api.md](docs/http-api.md)** for the full ACP API reference
and **[docs/openai-api.md](docs/openai-api.md)** for the OpenAI-compatible API reference.

## curl Example

```bash
# 1. Initialize
curl -s -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'

# → Returns Acp-Connection-Id header

# 2. Open SSE stream (in background)
curl -s -N http://localhost:8766/acp \
  -H "Accept: text/event-stream" \
  -H "Acp-Connection-Id: <conn_id>" &
```

See **[docs/http-api.md](docs/http-api.md)** for the full ACP API reference
and **[docs/openai-api.md](docs/openai-api.md)** for the OpenAI-compatible API reference.

## Architecture

```
Browser / Client
    │
    ├── POST /acp  (initialize, session/prompt, session/cancel)
    │       │
    ├── GET  /acp  (SSE stream — agent messages, tool calls, permissions)
    │       │
    └── DELETE /acp
            │
    ┌───────▼──────────────────────┐
    │  acp-http-gateway            │
    │  (aiohttp, transport proxy)  │
    └───────┬──────────────────────┘
            │ stdin / stdout
    ┌───────▼──────┐
    │  ACP Agent   │  (any stdio ACP agent)
    │  (subprocess) │
    └──────────────┘
```

## CLI Reference

```
usage: acp-http-gateway --cmd CMD [options]

ACP Streamable HTTP Gateway — bridges HTTP to stdio ACP agents

required arguments:
  --cmd CMD             ACP agent command (e.g. 'npx pi-acp')

options:
  --host HOST           Bind address (env: ACP_HTTP_HOST, default: 0.0.0.0)
  --port PORT           Bind port (env: ACP_HTTP_PORT, default: 8766)
  --max-capacity N      Max concurrent connections (default: 50)
  --idle-timeout S      Connection idle timeout in seconds (default: 300)
  --cors-origin ORIGIN  CORS origin (env: ACP_CORS_ORIGIN, e.g. "*")
  --bearer-token TOKEN  Require Bearer token auth (env: ACP_BEARER_TOKEN)
  --enable-openai       Expose POST /v1/chat/completions (env: ACP_ENABLE_OPENAI=1)
  --openai-pool-max N   Max pooled OpenAI sessions (default: 20)
  --openai-pool-idle S  OpenAI session idle timeout (default: 600)
  --log-level LEVEL     DEBUG, INFO, WARNING, ERROR (default: INFO)
```

## Python API

```python
import asyncio
from acp_http_gateway import run_server
from acp_http_gateway.auth import BearerTokenValidator

async def main():
    await run_server(
        cmd=["npx", "pi-acp"],
        host="0.0.0.0",
        port=8766,
        auth_validator=BearerTokenValidator("sk-secret"),
        cors_origin="*",
        max_capacity=50,
    )

asyncio.run(main())
```

## Supported Agents

Any agent that speaks ACP over stdio (line-delimited JSON-RPC on
stdin/stdout):

- `pi-acp` — `npx pi-acp` (Node.js adapter for [pi](https://github.com/badlogic/pi-mono))
- `codex-acp` — `npx -y @agentclientprotocol/codex-acp`
- Your own ACP agent — any stdio-compatible implementation

> **pi-acp note:** pi-acp spawns `pi --mode rpc`, which reads configuration
> from `~/.pi/agent/`.  The gateway automatically sets
> `PI_CODING_AGENT_DIR=~/.pi/agent` for the subprocess so custom providers
> (defined in `models.json`) and extensions are discovered correctly.

## Project Structure

```
acp-http-gateway/
├── src/acp_http_gateway/
│   ├── __init__.py          # Public API
│   ├── __main__.py          # CLI entry point
│   ├── server.py            # aiohttp HTTP server
│   ├── bridge.py            # Agent subprocess bridge
│   ├── connection.py        # Connection state + registry
│   ├── sse.py               # SSE formatting utilities
│   ├── auth.py              # Pluggable auth abstraction
│   └── openai/              # OpenAI-compatible layer (optional)
│       ├── handler.py       #   POST /v1/chat/completions
│       ├── openai_format.py #   OpenAI ↔ ACP format conversion
│       └── session_pool.py  #   LRU session pool
├── examples/
│   ├── simple_client.py     # Python client
│   └── browser-client.html  # Browser demo
├── docs/
│   └── http-api.md          # Full HTTP API reference
├── tests/
│   ├── test_server.py
│   └── test_openai.py
├── pyproject.toml
├── README.md
├── LICENSE
├── CONTRIBUTING.md
└── CHANGELOG.md
```

## Development

```bash
# Install with dev dependencies
uv sync --python /opt/homebrew/opt/python@3.12/bin/python3.12

# Run tests
uv run pytest

# Type check
uv run mypy src/
```

## License

Apache 2.0 — see [LICENSE](LICENSE).

[acp]: https://agentclientprotocol.com
[spec]: https://agentclientprotocol.com/rfds/streamable-http-websocket-transport
