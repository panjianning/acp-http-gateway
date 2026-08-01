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
"""OpenAI Chat Completions format helpers.

Converts between the OpenAI ``/v1/chat/completions`` wire format and the
ACP JSON-RPC / SSE event stream.

The OpenAI compatibility layer is intentionally **lossy**:

- ``tool_calls`` are not exposed — the agent executes its own toolchain
  and only the final text is returned.
- ``usage`` is not available from ACP and is always reported as zeros.
- ``system`` messages are only injected on the first turn of a new
  session (the agent owns its own system prompt).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

# ── Request parsing ────────────────────────────────────────────────


def parse_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an OpenAI chat completions request.

    Args:
        body: The parsed JSON body of ``POST /v1/chat/completions``.

    Returns:
        A normalized dict with keys:
            ``messages`` (list), ``model`` (str), ``stream`` (bool),
            ``session_id`` (str | None), ``cwd`` (str).

    Raises:
        ValueError: If ``messages`` is missing or empty.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' is required and must be a non-empty array")

    model = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    session_id = body.get("session_id")
    cwd = str(body.get("cwd") or "/tmp")

    return {
        "messages": messages,
        "model": model,
        "stream": stream,
        "session_id": session_id,
        "cwd": cwd,
    }


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the text of the last ``user`` message.

    Args:
        messages: The OpenAI ``messages`` array.

    Returns:
        The concatenated text content of the last user message,
        or empty string if none found.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            if parts:
                return " ".join(parts)
    return ""


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """Build an ACP prompt text from OpenAI messages.

    For a new session, all messages are rendered in order.  For an
    existing session only the last user message matters (history is
    already in the agent context); callers should pass only the last
    user message in that case.

    Args:
        messages: The OpenAI ``messages`` array.

    Returns:
        A plain-text prompt for ``session/prompt``.
    """
    return last_user_text(messages)


# ── Response formatting ────────────────────────────────────────────


def make_chat_id() -> str:
    """Generate an OpenAI-style completion ID."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def make_chunk(
    chat_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> dict[str, Any]:
    """Build one SSE chunk for a streaming response.

    Args:
        chat_id: The completion id.
        model: The model name echoed back.
        delta: The delta payload (``{"role": ...}`` or ``{"content": ...}``).
        finish_reason: ``"stop"``, ``"tool_calls"`` or ``None``.

    Returns:
        An OpenAI ``chat.completion.chunk`` object.
    """
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def make_response(
    chat_id: str,
    model: str,
    content: str,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Build a non-streaming completion response.

    Args:
        chat_id: The completion id.
        model: The model name echoed back.
        content: The assistant message text.
        finish_reason: ``"stop"`` by default.

    Returns:
        An OpenAI ``chat.completion`` object.
    """
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def make_error(message: str, code: str = "agent_error") -> dict[str, Any]:
    """Build an OpenAI-style error body.

    Args:
        message: The error message.
        code: A machine-readable error code.

    Returns:
        ``{"error": {"message": ..., "type": "invalid_request_error",
        "code": ...}}``.
    """
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": code,
        }
    }


def json_dumps(obj: Any) -> str:
    """Serialize an object to a compact JSON string for SSE framing."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
