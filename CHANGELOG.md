# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-08-01

### Added

- **Docker support**: all-in-one image (acp-http-gateway + pi + pi-acp).
  Dockerfile clones the gateway from GitHub and installs pi/pi-acp via
  npm; no local checkout needed.  Includes .env.example and
  docs/QUICKSTART.md.
- OpenAI Chat Completions compatibility layer:
  `POST /v1/chat/completions` (enable with `--enable-openai`).
- Stateful multi-turn sessions via `X-ACP-Session-Id` header and
  `session_id` request field.
- LRU session pool for the OpenAI layer (`--openai-pool-max`,
  `--openai-pool-idle`).
- Streaming responses (OpenAI SSE chunk format).
- Startup banner stripping for pi-acp agents.
- `tests/test_openai.py` with 5 tests.

### Fixed

- Suppress extension `ui.notify` output in headless agent subprocesses
  (gateway sets `PI_EXT_QUIET=1`), preventing extension text from
  polluting OpenAI/SSE responses.
- CORS `Access-Control-Allow-Origin` missing on `GET /acp` SSE responses.
- CORS `Access-Control-Expose-Headers` missing for `Acp-Connection-Id`.
- Client `Content-Type` header bug (`{CONTENT_TYPE: CONTENT_TYPE}`).
- SSE event parsing in examples (`params.update.content`, not
  `params.message`).
- Browser demo event-ordering bug where `sessionId` was never captured.

## [0.1.0] — 2026-07-29

### Added

- Initial release.
- `POST /acp` endpoint — send JSON-RPC messages to stdio ACP agents.
- `GET /acp` endpoint — SSE stream for server→client messages
  (connection-scoped and session-scoped).
- `DELETE /acp` endpoint — terminate connections.
- `GET /health` endpoint — liveness probe.
- Pluggable authentication with `AuthValidator` abstract class.
- Built-in `BearerTokenValidator` for simple token auth.
- `NoAuthValidator` for development/trusted environments.
- CORS support via `--cors-origin`.
- Connection capacity limiting (`--max-capacity`).
- Idle connection cleanup (`--idle-timeout`).
- Cookie-based connection lookup for browser SSE compatibility.
- Python client example (`examples/simple_client.py`).
- Browser demo page (`examples/browser-client.html`).
- Full HTTP API reference documentation (`docs/http-api.md`).

[0.1.0]: https://github.com/example/acp-http-gateway/releases/tag/v0.1.0
