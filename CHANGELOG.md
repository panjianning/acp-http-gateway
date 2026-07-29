# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
