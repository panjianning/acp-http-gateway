# ACP Streamable HTTP Gateway — HTTP API Reference

This document specifies every HTTP endpoint, header, and message format
for the gateway.  Use it to integrate ACP with **any HTTP-capable client**
— `curl`, `fetch()`, Python `aiohttp`, Go `net/http`, etc.

No SDK is required.  Just HTTP + JSON-RPC.

---

## Table of Contents

- [1. Quick Start (curl)](#1-quick-start-curl)
- [2. Endpoint Overview](#2-endpoint-overview)
- [3. Authentication](#3-authentication)
- [4. POST /acp](#4-post-acp)
  - [4.1 initialize](#41-initialize)
  - [4.2 session/new](#42-sessionnew)
  - [4.3 session/prompt](#43-sessionprompt)
  - [4.4 session/cancel](#44-sessioncancel)
  - [4.5 Permission Response](#45-permission-response)
  - [4.6 session/list](#46-sessionlist)
  - [4.7 session/load](#47-sessionload)
- [5. GET /acp (SSE Stream)](#5-get-acp-sse-stream)
  - [5.1 Connection-Scoped Stream](#51-connection-scoped-stream)
  - [5.2 Session-Scoped Stream](#52-session-scoped-stream)
  - [5.3 SSE Event Format](#53-sse-event-format)
- [6. DELETE /acp](#6-delete-acp)
- [7. OpenAI Compatibility (`POST /v1/chat/completions`)](#7-openai-compatibility-post-v1chatcompletions)
- [8. Error Responses](#8-error-responses)
- [9. Complete Flow Examples](#9-complete-flow-examples)
- [10. Headers Reference](#10-headers-reference)

---

## 1. Quick Start (curl)

```bash
# 1. Start the gateway (terminal 1)
acp-http-gateway --cmd "npx pi-acp" --cors-origin "*"

# 2. Initialize
curl -s -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": 1,
      "clientCapabilities": {},
      "clientInfo": {"name": "curl", "version": "1.0"}
    }
  }'

# → 200 OK
# → Acp-Connection-Id: a1b2c3d4e5f6g7h8
# → {"jsonrpc":"2.0","id":1,"result":{"connectionId":"a1b2c3d4e5f6g7h8",...}}
```

```bash
# 3. Open SSE stream (in background)
curl -s -N http://localhost:8766/acp \
  -H "Accept: text/event-stream" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" &
SSE_PID=$!
```

```bash
# 4. Create session
curl -s -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/new",
    "id": 2,
    "params": {"cwd": "/tmp"}
  }'

# → 202 Accepted (response arrives on SSE stream)
```

```bash
# 5. Send prompt (replace SESSION_ID from SSE stream output)
curl -s -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -H "Acp-Session-Id: SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/prompt",
    "id": 3,
    "params": {
      "sessionId": "SESSION_ID",
      "prompt": [{"type": "text", "text": "Hello, world!"}]
    }
  }'

# → 202 Accepted
# → Response streams on SSE
```

```bash
# 6. Cleanup
curl -s -X DELETE http://localhost:8766/acp \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8"
kill $SSE_PID
```

---

## 2. Endpoint Overview

| Method   | Path  | Purpose                                           | Response       |
|----------|-------|---------------------------------------------------|----------------|
| `POST`   | `/acp` | Send JSON-RPC message to agent                   | 200 or 202     |
| `GET`    | `/acp` | Open SSE stream for server→client messages       | SSE stream     |
| `DELETE` | `/acp` | Terminate the connection                          | 202            |
| `GET`    | `/health` | Liveness probe                                 | 200 + JSON     |

---

## 3. Authentication

Authentication happens **once** during the `initialize` POST.  The gateway
supports:

### 3.1 Bearer Token (built-in)

```bash
curl -X POST http://localhost:8766/acp \
  -H "Authorization: Bearer sk-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{...}}'
```

Start the gateway with:

```bash
acp-http-gateway --cmd "..." --bearer-token "sk-secret-token"
```

### 3.2 Custom Auth (via Python API)

```python
from acp_http_gateway import run_server
from acp_http_gateway.auth import AuthValidator

class MyAuth(AuthValidator):
    async def validate(self, request):
        api_key = request.headers.get("X-Api-Key")
        if api_key == "expected": return {"user": "admin"}
        return None

await run_server(cmd=["..."], auth_validator=MyAuth())
```

### 3.3 No Auth

Omit `--bearer-token` and the gateway allows all connections.  This is
suitable for local development or when auth is handled by a reverse proxy.

---

## 4. POST /acp

Send a JSON-RPC 2.0 message to the agent.

**Request Headers:**

| Header               | Required              | Value                  |
|----------------------|-----------------------|------------------------|
| `Content-Type`       | **Yes**               | `application/json`     |
| `Authorization`      | If auth is configured | `Bearer <token>`       |
| `Acp-Connection-Id`  | After `initialize`    | `<connection_id>`      |
| `Acp-Session-Id`     | Session-scoped ops    | `<session_id>`         |

**Request Body:** A JSON-RPC 2.0 object with `jsonrpc`, `method`, `id`, and
optional `params`.

```json
{
  "jsonrpc": "2.0",
  "method": "session/prompt",
  "id": 3,
  "params": {
    "sessionId": "sess_abc",
    "prompt": [{"type": "text", "text": "Hello"}]
  }
}
```

### 4.1 initialize

The first POST — establishes a connection.

| Attribute           | Detail                          |
|---------------------|---------------------------------|
| **Method**          | `initialize`                    |
| **Response status** | `200 OK`                        |
| **Response body**   | JSON-RPC response with capabilities |
| **Response headers**| `Acp-Connection-Id`, `Set-Cookie` |

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": 1,
      "clientCapabilities": {},
      "clientInfo": {"name": "my-app", "version": "1.0"}
    }
  }'
```

**Response (200):**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": 1,
    "connectionId": "a1b2c3d4e5f6g7h8",
    "agentCapabilities": {
      "promptCapabilities": {"text": true, "image": false}
    },
    "agentInfo": {"name": "pi-acp", "version": "0.2.4"}
  }
}
```

**Response Headers:**

```
Acp-Connection-Id: a1b2c3d4e5f6g7h8
Set-Cookie: acp_conn=a1b2c3d4e5f6g7h8; HttpOnly; SameSite=Lax; Path=/
Content-Type: application/json
```

> **Important:** Save `Acp-Connection-Id` — you need it for **every subsequent
> request**.  The cookie is set so that browsers can access the SSE stream
> without custom headers (browser `EventSource` limitation).

**Auth failure (401):**

```json
{"error": "Authentication required"}
```

### 4.2 session/new

Create an agent session.  The response arrives on the **connection-scoped**
SSE stream.

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/new",
    "id": 2,
    "params": {"cwd": "/tmp"}
  }'
```

**Response (202 Accepted):** Empty body.

**SSE event (on connection-scoped stream):**

```
event: message
data: {"jsonrpc":"2.0","id":2,"result":{"sessionId":"sess_abc123","modes":[...]}}
```

### 4.3 session/prompt

Send a user prompt to the agent.

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -H "Acp-Session-Id: sess_abc123" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/prompt",
    "id": 3,
    "params": {
      "sessionId": "sess_abc123",
      "prompt": [{"type": "text", "text": "What is the weather?"}]
    }
  }'
```

**Response (202 Accepted):** Empty body.

**SSE events (on session-scoped stream):**

```
event: message
data: {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess_abc123","sessionUpdate":"agent_message_chunk","message":{"role":"assistant","content":"Let me check..."}}}

event: message
data: {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess_abc123","sessionUpdate":"tool_call","toolCall":{"toolCallId":"call_1","title":"get_weather","status":"pending"}}}

event: message
data: {"jsonrpc":"2.0","id":3,"result":{"sessionId":"sess_abc123","stopReason":"end_turn"}}
```

### 4.4 session/cancel

Cancel the current turn.  No `id` field (notification).

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -H "Acp-Session-Id: sess_abc123" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/cancel",
    "params": {"sessionId": "sess_abc123"}
  }'
```

**Response (202 Accepted):** Empty body.

### 4.5 Permission Response

When the agent needs user approval for a tool call, it sends a
`request_permission` on the SSE stream.  The client responds with a POST.

**SSE event (server → client):**

```
event: message
data: {"jsonrpc":"2.0","method":"request_permission","id":99,"params":{"sessionId":"sess_abc123","toolCall":{"toolCallId":"call_1","title":"rm -rf /tmp/test","kind":"other","status":"pending"}}}
```

**Client response (POST):**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -H "Acp-Session-Id: sess_abc123" \
  -d '{
    "jsonrpc": "2.0",
    "id": 99,
    "result": {"outcome": "allow_once"}
  }'
```

**Outcome values:**

| `outcome`        | Meaning                          |
|------------------|----------------------------------|
| `allow_once`     | Execute this tool call once      |
| `reject_once`    | Reject this tool call            |
| `allow_always`   | Allow all similar tool calls     |
| `reject_always`  | Reject all similar tool calls    |

### 4.6 session/list

List all sessions known to the agent.  The response is delivered via the
**connection-scoped** SSE stream.

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: <conn_id>" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/list",
    "id": 3,
    "params": {}
  }'
```

**SSE Response** (on the connection-scoped stream):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "sessions": [
      {
        "sessionId": "019fadd6-1730-7950-959c-7247e574a9b6",
        "cwd": "/Users/alice/work",
        "title": "Hello, world!",
        "updatedAt": "2026-07-29T12:25:27.655Z"
      }
    ],
    "nextCursor": null
  }
}
```

| Field         | Type     | Description                         |
|---------------|----------|-------------------------------------|
| `sessions`    | array    | List of `SessionInfo` objects       |
| `nextCursor`  | string?  | Cursor for the next page, or `null` |

### 4.7 session/load

Load a previously-created session.  History messages are **replayed** as
`session/update` events on the SSE stream *before* the load response.

**Request:**

```bash
curl -X POST http://localhost:8766/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: <conn_id>" \
  -d '{
    "jsonrpc": "2.0",
    "method": "session/load",
    "id": 4,
    "params": {
      "sessionId": "019fadd6-1730-7950-959c-7247e574a9b6",
      "cwd": "/Users/alice/work",
      "mcpServers": []
    }
  }'
```

**SSE Events** (on the connection-scoped stream, in order):

```
event: message
data: {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"...","update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"Hi"}}}}

event: message
data: {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"...","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Hello!"}}}}

event: message
data: {"jsonrpc":"2.0","id":4,"result":{"configOptions":[...],"models":{...}}}
```

1. **User & agent messages** are replayed as `session/update` events.
2. **The load response** (`id: 4, result: {...}`) signals completion.
   After this, you can send `session/prompt` to continue the conversation.

---

## 5. GET /acp (SSE Stream)

Open a long-lived SSE (Server-Sent Events) stream to receive messages from
the agent.

**Request Headers:**

| Header              | Required | Value                  |
|---------------------|----------|------------------------|
| `Accept`            | **Yes**  | `text/event-stream`    |
| `Acp-Connection-Id` | **Yes**\* | `<connection_id>`    |
| `Acp-Session-Id`    | Optional | `<session_id>`         |

\* For browser clients that cannot set custom headers, the `acp_conn` cookie
(set during `initialize`) is also accepted.

### 5.1 Connection-Scoped Stream

Omitting `Acp-Session-Id` opens a stream that receives:

- Responses to `session/new` and `session/load`
- Server-initiated messages not tied to a specific session

```bash
curl -s -N http://localhost:8766/acp \
  -H "Accept: text/event-stream" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8"
```

### 5.2 Session-Scoped Stream

Including `Acp-Session-Id` opens a stream that receives:

- `session/update` notifications (agent messages, tool calls, plans)
- `request_permission` (server→client requests)
- Responses to `session/prompt`, `session/cancel`

```bash
curl -s -N http://localhost:8766/acp \
  -H "Accept: text/event-stream" \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8" \
  -H "Acp-Session-Id: sess_abc123"
```

> **Note for browser clients:** Use `fetch()` with `ReadableStream` (not
> `EventSource`) when you need custom headers.  Alternatively, rely on the
> `acp_conn` cookie set by `initialize` and use `EventSource` for same-origin
> connections.

### 5.3 SSE Event Format

```
event: message
data: {"jsonrpc":"2.0",...}

: heartbeat
```

- The `event` field is always `message`.
- The `data` field is a single-line JSON object (or multi-line if the JSON
  contains newlines in string values).
- Lines starting with `:` are **comments** (heartbeats).  Ignore them.
- Events are separated by a blank line.

---

## 6. DELETE /acp

Terminate the connection and kill the agent subprocess.

**Request:**

```bash
curl -X DELETE http://localhost:8766/acp \
  -H "Acp-Connection-Id: a1b2c3d4e5f6g7h8"
```

**Response:** `202 Accepted` with empty body.

All active SSE streams for this connection will close.

---

## 7. OpenAI Compatibility (`POST /v1/chat/completions`)

*Enabled with `--enable-openai`.*  Exposes an OpenAI Chat Completions
compatible endpoint on top of the ACP agent.  This lets any OpenAI SDK,
tool, or script talk to the agent without knowing ACP.

> Full reference: **[docs/openai-api.md](openai-api.md)** (request/response
> schema, stateful sessions, OpenAI SDK examples, limitations).
> Below is a summary.

```bash
# Enable
acp-http-gateway --cmd "npx pi-acp" --enable-openai
```

**Request body** (subset of the OpenAI schema):

```json
{
  "model": "example/deepseek-v4-pro",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"}
  ],
  "stream": false,
  "session_id": "019fbb48-ae22-7360-858c-9e1be5d8db55"
}
```

| Field        | Required | Notes                                          |
|--------------|----------|------------------------------------------------|
| `messages`   | **Yes**  | Array of `{role, content}`.  Last user message becomes the prompt. |
| `model`      | No       | Echoed back; switches the agent model via `session/set_config_option`.  |
| `stream`     | No       | `true` returns OpenAI SSE chunks.              |
| `session_id` | No       | Reuse an existing session (from `X-ACP-Session-Id`). |

**Non-streaming response:**

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1785553150,
  "model": "example/deepseek-v4-pro",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

The response includes the `X-ACP-Session-Id` header — pass it back as
`session_id` on the next request to continue the same conversation.

**Streaming response** (when `stream: true`):

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

**Behavior notes:**

- Tool calls are **not** exposed — the agent runs its own toolchain and
  only the final text is returned.
- The agent's startup banner (e.g. pi's "pi v0.82.1 / ## Skills" prelude)
  is stripped from the first reply.
- Sessions are pooled (`--openai-pool-max`, `--openai-pool-idle`).  Idle
  sessions are evicted; an evicted `session_id` transparently starts a
  fresh session.

---

## 8. Error Responses

| Status | Meaning                             | Body                                   |
|--------|-------------------------------------|----------------------------------------|
| 400    | Bad request (missing header, etc.)  | `{"error": "..."}`                     |
| 401    | Auth required / invalid credentials | `{"error": "Authentication required"}` |
| 404    | Unknown connection or session       | `{"error": "Unknown Acp-Connection-Id"}` |
| 406    | Wrong Accept header (GET)           | `Not Acceptable: ...`                  |
| 415    | Wrong Content-Type (POST)           | `Unsupported Media Type: ...`          |
| 502    | Agent subprocess error              | `{"error": "..."}`                     |
| 503    | Server at capacity                  | `{"error": "Server at capacity..."}`    |

---

## 9. Complete Flow Examples

### 9.1.1 curl (Full Script)

```bash
#!/bin/bash
set -e
BASE="http://localhost:8766"

# 1. Initialize
RESP=$(curl -s -X POST "$BASE/acp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"test","version":"1"}}}')

CONN_ID=$(echo "$RESP" | grep -o '"connectionId":"[^"]*"' | cut -d'"' -f4)
echo "Connection: $CONN_ID"

# 2. Open SSE stream (background)
curl -s -N "$BASE/acp" \
  -H "Accept: text/event-stream" \
  -H "Acp-Connection-Id: $CONN_ID" > /tmp/sse.log 2>&1 &
SSE_PID=$!
sleep 1

# 3. Create session
curl -s -X POST "$BASE/acp" \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","method":"session/new","id":2,"params":{"cwd":"/tmp","mcpServers":[]}}' > /dev/null
sleep 1

# Read session ID from SSE log
SESSION_ID=$(grep -o '"sessionId":"[^"]*"' /tmp/sse.log | head -1 | cut -d'"' -f4)
echo "Session: $SESSION_ID"

# 4. Send prompt
curl -s -X POST "$BASE/acp" \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -H "Acp-Session-Id: $SESSION_ID" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"session/prompt\",\"id\":3,\"params\":{\"sessionId\":\"$SESSION_ID\",\"prompt\":[{\"type\":\"text\",\"text\":\"Hello\"}]}}" > /dev/null

# 5. Wait + show output
sleep 5
echo "=== SSE Output ==="
cat /tmp/sse.log

# 6. Cleanup
kill $SSE_PID 2>/dev/null
curl -s -X DELETE "$BASE/acp" -H "Acp-Connection-Id: $CONN_ID" > /dev/null
rm -f /tmp/sse.log
```

### 9.2.2 JavaScript (fetch)

```javascript
const BASE = 'http://localhost:8766';

// 1. Initialize
const initResp = await fetch(`${BASE}/acp`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    jsonrpc: '2.0', method: 'initialize', id: 1,
    params: { protocolVersion: 1, clientCapabilities: {},
              clientInfo: { name: 'js', version: '1' } }
  })
});
const connId = initResp.headers.get('Acp-Connection-Id');

// 2. Open SSE stream
const sseResp = await fetch(`${BASE}/acp`, {
  headers: { 'Accept': 'text/event-stream', 'Acp-Connection-Id': connId }
});
const reader = sseResp.body.getReader();

// Read SSE asynchronously
(async () => {
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += new TextDecoder().decode(value, { stream: true });
    // Parse SSE events from buffer...
  }
})();

// 3. Create session
await fetch(`${BASE}/acp`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Acp-Connection-Id': connId },
  body: JSON.stringify({
    jsonrpc: '2.0', method: 'session/new', id: 2,
    params: { cwd: '/tmp' }
  })
});
```

### 9.3.3 Python (aiohttp)

See [`examples/simple_client.py`](../examples/simple_client.py) for a
complete, runnable Python client.

---

## 10. Headers Reference

| Header                 | Direction        | When                    | Value                         |
|------------------------|------------------|-------------------------|-------------------------------|
| `Content-Type`         | Client → Server  | All POST                | `application/json`            |
| `Accept`               | Client → Server  | All GET (SSE)           | `text/event-stream`           |
| `Authorization`        | Client → Server  | `initialize` POST       | `Bearer <token>`              |
| `Acp-Connection-Id`    | Both             | After `initialize`      | `<conn_id>` (16 hex chars)    |
| `Acp-Session-Id`       | Both             | Session-scoped ops      | `<session_id>`                |
| `Set-Cookie`           | Server → Client  | `initialize` response   | `acp_conn=<conn_id>; ...`     |
| `Access-Control-Allow-Origin` | Server → Client | All (if CORS enabled) | `*` or origin               |

---

## References

- [ACP Protocol v1 Specification](https://agentclientprotocol.com/protocol/v1/overview)
- [Streamable HTTP & WebSocket Transport (RFD)](https://agentclientprotocol.com/rfds/streamable-http-websocket-transport)
- [JSON-RPC 2.0 Specification](https://www.jsonrpc.org/specification)
- [Server-Sent Events (SSE) Specification](https://html.spec.whatwg.org/multipage/server-sent-events.html)
