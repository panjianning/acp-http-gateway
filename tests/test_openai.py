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
"""Tests for the OpenAI Chat Completions compatibility layer."""

from __future__ import annotations

import json

import aiohttp
import pytest

from acp_http_gateway.auth import NoAuthValidator
from acp_http_gateway.server import create_app

# ── Fake agent that speaks the ACP subset needed by the OpenAI layer ──

_OPENAI_FAKE_AGENT = r"""
import sys, json

def send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

BANNER = "pi v0.1.0\n---\n\n## Skills\n- fake-skill"

for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method", "")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":req["id"],"result":{"protocolVersion":1,"agentInfo":{"name":"fake","version":"1.0"}}})
    elif method == "session/new":
        send({"jsonrpc":"2.0","id":req["id"],"result":{"sessionId":"acp-sess-001","_meta":{"piAcp":{"startupInfo":BANNER}}}})
    elif method == "session/prompt":
        sid = req["params"]["sessionId"]
        first = req["params"]["prompt"][0]["text"]
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":BANNER}}}})
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Reply to: " + first}}}})
        send({"jsonrpc":"2.0","id":req["id"],"result":{"sessionId":sid,"stopReason":"end_turn"}})
    elif method == "session/set_config_option":
        value = req["params"].get("value", "")
        if value == "bad-model":
            send({"jsonrpc":"2.0","id":req["id"],"error":{"code":-32602,"message":"Unknown modelId: bad-model"}})
        else:
            send({"jsonrpc":"2.0","id":req["id"],"result":{}})
    else:
        send({"jsonrpc":"2.0","id":req["id"],"result":{}})
"""


def _openai_fake_cmd() -> list[str]:
    return ["python3", "-c", _OPENAI_FAKE_AGENT]


@pytest.fixture
async def openai_base_url():
    """Server with the OpenAI layer enabled."""
    app = create_app(
        _openai_fake_cmd(),
        auth_validator=NoAuthValidator(),
        enable_openai=True,
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

    pool = app["openai_pool"]
    await pool.evict_all()
    await runner.cleanup()


@pytest.fixture
async def session():
    """aiohttp client session."""
    async with aiohttp.ClientSession() as s:
        yield s


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_chat_completion(openai_base_url, session):
    """POST /v1/chat/completions returns a non-streaming completion."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body
    ) as resp:
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Reply to: hello"
        assert data["choices"][0]["finish_reason"] == "stop"
        # Session id is returned in the response header
        assert resp.headers.get("X-ACP-Session-Id")


@pytest.mark.asyncio
async def test_openai_strips_startup_banner(openai_base_url, session):
    """The agent's startup banner is dropped from the first reply.

    The fake agent returns ``_meta.piAcp.startupInfo`` from
    ``session/new`` and emits it as the first ``agent_message_chunk``
    before the real reply.  The OpenAI layer must strip that chunk.
    """
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body
    ) as resp:
        assert resp.status == 200
        data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        # Banner text must not leak into the reply.
        assert "## Skills" not in content
        assert content == "Reply to: hello"


@pytest.mark.asyncio
async def test_openai_chat_streaming(openai_base_url, session):
    """POST /v1/chat/completions with stream=true returns SSE chunks."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")

        text = await resp.text()
        chunks = [
            line[6:]
            for line in text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"
        ]
        assert chunks, "expected at least one data chunk"

        # First chunk should carry the role
        first = json.loads(chunks[0])
        assert first["object"] == "chat.completion.chunk"
        assert first["choices"][0]["delta"]["role"] == "assistant"

        # Reconstruct content from content deltas
        content = ""
        for c in chunks:
            delta = json.loads(c)["choices"][0]["delta"]
            if "content" in delta:
                content += delta["content"]
        assert content == "Reply to: hello"


@pytest.mark.asyncio
async def test_openai_stateful_session(openai_base_url, session):
    """Same X-ACP-Session-Id reuses the pooled session (no new session/new)."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "turn one"}],
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body
    ) as resp:
        assert resp.status == 200
        sid = resp.headers.get("X-ACP-Session-Id")
        assert sid

    # Second turn with the same session id
    body2 = {
        "model": "m",
        "messages": [{"role": "user", "content": "turn two"}],
        "session_id": sid,
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body2
    ) as resp2:
        assert resp2.status == 200
        data = await resp2.json()
        assert data["choices"][0]["message"]["content"] == "Reply to: turn two"


@pytest.mark.asyncio
async def test_openai_missing_messages(openai_base_url, session):
    """400 when messages is missing."""
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json={"model": "m"}
    ) as resp:
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data


@pytest.mark.asyncio
async def test_openai_unknown_model_404(openai_base_url, session):
    """Unknown model returns 404 with an OpenAI-style error."""
    body = {
        "model": "bad-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with session.post(
        f"{openai_base_url}/v1/chat/completions", json=body
    ) as resp:
        assert resp.status == 404
        data = await resp.json()
        assert data["error"]["code"] == "model_not_found"
        assert "bad-model" in data["error"]["message"]
