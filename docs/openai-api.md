# acp-http-gateway — OpenAI Chat Completions API

This document describes the **OpenAI-compatible** HTTP API exposed by
`acp-http-gateway` when launched with `--enable-openai`.

It implements the [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
subset, backed by an ACP agent (e.g. pi-acp) instead of a model endpoint.
Any OpenAI SDK, tool, or script can talk to the agent without knowing ACP.

---

## 1. Enabling

```bash
# Start the gateway with the OpenAI layer enabled
acp-http-gateway --cmd "npx pi-acp" --enable-openai

# Optional tuning
acp-http-gateway --cmd "npx pi-acp" --enable-openai \
  --openai-pool-max 20 \      # max concurrent sessions (default 20)
  --openai-pool-idle 600      # idle timeout seconds (default 600)
```

Environment variables: `ACP_ENABLE_OPENAI=1`, `ACP_OPENAI_POOL_MAX`,
`ACP_OPENAI_POOL_IDLE`.

---

## 2. Endpoint

| Method | Path                       | Description                          |
|--------|----------------------------|--------------------------------------|
| POST   | `/v1/chat/completions`     | Send a chat completion request       |

Base URL for OpenAI SDKs: `http://localhost:8766/v1`

---

## 3. Request

### 3.1 Body

```json
{
  "model": "example/deepseek-v4-pro",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello, who are you?"}
  ],
  "stream": false,
  "session_id": "019fbb48-ae22-7360-858c-9e1be5d8db55"
}
```

### 3.2 Fields

| Field        | Required | Type    | Notes                                              |
|--------------|----------|---------|----------------------------------------------------|
| `messages`   | **Yes**  | array   | Array of `{role, content}`. The **last user message** becomes the prompt sent to the agent. |
| `model`      | No       | string  | Echoed back; switches the agent model via `session/set_config_option`. |
| `stream`     | No       | boolean | `true` → SSE chunk stream; `false` (default) → single JSON. |
| `session_id` | No       | string  | Reuse a session obtained from the `X-ACP-Session-Id` response header. |

Other OpenAI fields (`temperature`, `max_tokens`, `top_p`, `tools`, …)
are **ignored** — the agent controls its own sampling and toolchain.

### 3.3 Message roles

| Role      | Behavior                                                     |
|-----------|--------------------------------------------------------------|
| `system`  | Included in the prompt context on a **new** session only.    |
| `user`    | The last user message is sent to the agent.                  |
| `assistant` | Used as context on a new session (history replay).         |
| `tool`    | Not supported (tool calls are not exposed).                  |

---

## 4. Response

### 4.1 Non-streaming (`stream: false`)

```json
{
  "id": "chatcmpl-f249d76a64524f60903ce155",
  "object": "chat.completion",
  "created": 1785553150,
  "model": "example/deepseek-v4-pro",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "I'm an AI assistant running inside pi."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### 4.2 Response headers

| Header             | Value                                          |
|--------------------|------------------------------------------------|
| `X-ACP-Session-Id` | The session id. **Pass it back as `session_id`** to continue the conversation. |

### 4.3 Streaming (`stream: true`)

`Content-Type: text/event-stream`. Standard OpenAI chunk sequence:

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

The `X-ACP-Session-Id` header is present on the streaming response too.

---

## 5. Stateful sessions

The gateway pools agent connections so a conversation can span multiple
requests. This is how it works:

```
Request 1 (no session_id)
  → gateway spawns agent, session/new, prompt
  → response header: X-ACP-Session-Id: 019f...
  → session stored in pool

Request 2 (session_id: 019f...)
  → gateway reuses the pooled connection + session
  → agent remembers prior turns
  → response header: X-ACP-Session-Id: 019f...
```

```bash
# Turn 1
curl -s -D - http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"记住一个秘密数字 42"}]}'
# → X-ACP-Session-Id: 019fbb48-...  (grab it)

# Turn 2 — same conversation
curl -s http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role":"user","content":"刚才的秘密数字是什么？"}],
    "session_id": "019fbb48-..."
  }'
# → "42"
```

### 5.1 Pool lifecycle

- 1 session = 1 agent process.
- Idle sessions are evicted after `--openai-pool-idle` (default 600s).
- When the pool is full, the least-recently-used session is evicted.
- If a client returns with an evicted `session_id`, the gateway
  transparently starts a **new** session (the old conversation is lost;
  ACP session restore from disk is not yet wired into the OpenAI layer).

---

## 6. Using OpenAI SDKs

Point any OpenAI-compatible SDK at the gateway's `/v1`:

```bash
# Python (openai)
export OPENAI_BASE_URL=http://localhost:8766/v1
export OPENAI_API_KEY=anything  # gateway ignores the key by default
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8766/v1", api_key="dummy")

# Non-streaming
resp = client.chat.completions.create(
    model="example/deepseek-v4-pro",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)

# Streaming
stream = client.chat.completions.create(
    model="example/deepseek-v4-pro",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

> The `openai` Python SDK does not expose `session_id` directly. For
> stateful conversations with the official SDK, use a custom header or
> manage the session id yourself (grab `X-ACP-Session-Id` from the raw
> response and send it via `extra_body={"session_id": ...}`).

---

## 7. curl reference

```bash
BASE=http://localhost:8766

# Non-streaming
curl -s $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "example/deepseek-v4-pro",
    "messages": [{"role": "user", "content": "你好"}]
  }'

# Streaming
curl -s -N $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'

# With bearer auth (if gateway started with --bearer-token)
curl -s $BASE/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-secret" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

---

## 8. Behavior notes & limitations

- **Tool calls are not exposed.** The agent executes its own toolchain
  (bash, file edits, MCP, etc.) and only the final text reply is returned.
- **`usage` is always zero** — ACP has no token accounting.
- **Startup banners are stripped.** pi's "pi v0.82.1 / ## Skills" prelude
  is removed from the first reply of a session.
- **Extension UI notifications are suppressed.** The gateway sets
  `PI_EXT_QUIET=1` for agent subprocesses so extensions that call
  `ctx.ui.notify` (e.g. a custom system-prompt logger) don't leak text
  into responses.
- **`system` messages are only honored on a new session.**
- **No auth by default** on the OpenAI endpoint unless `--bearer-token`
  is set (the same token is then required on `/v1/chat/completions`).

---

## 9. Errors

Non-2xx responses use the OpenAI error shape:

```json
{
  "error": {
    "message": "Failed to create session: ...",
    "type": "invalid_request_error",
    "code": "agent_error"
  }
}
```

| Status | Meaning                              |
|--------|--------------------------------------|
| 400    | Malformed JSON, missing `messages`, no user message |
| 401    | Missing/invalid bearer token         |
| 502    | Agent spawn, session creation, or prompt failure |
