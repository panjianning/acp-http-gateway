# Contributing to acp-http-gateway

Thank you for your interest in contributing!  This document outlines the
process and conventions for contributing to this project.

## Code of Conduct

Be respectful, constructive, and collaborative.  Follow the
[Contributor Covenant](https://www.contributor-covenant.org/).

## Getting Started

```bash
git clone https://github.com/example/acp-http-gateway.git
cd acp-http-gateway

# Python 3.12+ via Homebrew
uv sync --python /opt/homebrew/opt/python@3.12/bin/python3.12
```

## Development Workflow

1. **Fork** the repository and create a branch from `main`.
2. **Make changes** — follow the conventions below.
3. **Write tests** for new functionality.
4. **Run tests** — `uv run pytest`.
5. **Submit a pull request** with a clear description.

## Coding Conventions

### Python

- Python 3.12+.
- All public functions must have **type annotations** and **docstrings**
  (Google style).
- Use `from __future__ import annotations` in every file.
- Log with `logging.getLogger(__name__)`, never `print()`.
- Imports order: `__future__`, stdlib, third-party, internal.

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from .bridge import spawn_agent
```

### Testing

- Use `pytest` with `pytest-asyncio`.
- Name test files `test_*.py` under `tests/`.
- Name test functions `test_*`.

### Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat: add session resumption support
fix: handle subprocess crash during initialize
docs: update API reference with cancel flow
```

## Project Structure

```
src/acp_http_gateway/
├── __init__.py          # Public API exports
├── __main__.py          # CLI entry point
├── server.py            # aiohttp application + routes
├── bridge.py            # Agent subprocess lifecycle
├── connection.py        # Connection state management
├── sse.py               # SSE formatting
└── auth.py              # Auth abstraction
```

## Pull Request Checklist

- [ ] Type annotations on all public methods.
- [ ] Docstrings on all public methods.
- [ ] Tests pass: `uv run pytest`.
- [ ] No `print()` calls in library code — use `logging`.
- [ ] Updated `CHANGELOG.md` with your change.
- [ ] PR description explains the "what" and "why".

## Questions?

Open an issue or discussion.
