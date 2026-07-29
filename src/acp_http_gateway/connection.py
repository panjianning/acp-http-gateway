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
"""Connection state and lifecycle management.

Each ACP client connection is represented by a :class:`Connection` object
that tracks:

- The spawned agent subprocess
- Auth context
- Active SSE queues (connection-scoped + per-session)
- Connection creation time (for TTL / garbage collection)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class Connection:
    """State for a single ACP client connection.

    Created during the ``initialize`` POST and destroyed via ``DELETE /acp``
    or after a configurable idle timeout.

    Attributes:
        connection_id: Unique identifier (returned to client as
            ``Acp-Connection-Id`` header).
        auth_context: Auth metadata from the :class:`AuthValidator`.
        proc: The spawned agent subprocess (``asyncio.subprocess.Process``).
        stdin: The subprocess stdin stream writer.
        sse_queues: Mapping of session ID (or ``None`` for
            connection-scoped) to ``asyncio.Queue`` for SSE delivery.
        created_at: ``time.monotonic()`` timestamp for TTL tracking.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        stdin: asyncio.StreamWriter,
        stderr_task: asyncio.Task[None],
        auth_context: dict[str, Any],
    ) -> None:
        self.connection_id: str = uuid.uuid4().hex[:16]
        self.auth_context: dict[str, Any] = auth_context
        self.proc: asyncio.subprocess.Process = proc
        self.stdin: asyncio.StreamWriter = stdin
        self._stderr_task: asyncio.Task[None] = stderr_task
        self.sse_queues: dict[str | None, asyncio.Queue[dict[str, Any]]] = {}
        self.created_at: float = time.monotonic()
        self._closed: bool = False

    def close(self) -> None:
        """Shut down this connection: kill subprocess, cancel stderr task."""
        if self._closed:
            return
        self._closed = True
        try:
            self.stdin.close()
        except Exception:
            pass
        if not self._stderr_task.done():
            self._stderr_task.cancel()
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    async def wait_closed(self, timeout: float = 5.0) -> None:
        """Wait for the subprocess to exit gracefully.

        Args:
            timeout: Seconds to wait before force-killing.
        """
        self.close()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            await self.proc.wait()
        except ProcessLookupError:
            pass

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed


class ConnectionStore:
    """Thread-safe registry of active connections.

    Tracks connections by their ``Acp-Connection-Id`` and supports
    optional TTL-based garbage collection for abandoned connections.
    """

    def __init__(self, idle_timeout: float = 300.0) -> None:
        """Initialize the store.

        Args:
            idle_timeout: Seconds of inactivity before a connection is
                eligible for cleanup (0 = never expire).
        """
        self._connections: dict[str, Connection] = {}
        self._idle_timeout = idle_timeout

    def add(self, conn: Connection) -> None:
        """Register a new connection.

        Args:
            conn: The connection to register.
        """
        self._connections[conn.connection_id] = conn
        logger.info(
            "Connection %s created (total=%d, pid=%d)",
            conn.connection_id,
            len(self._connections),
            conn.proc.pid,
        )

    def get(self, connection_id: str) -> Connection | None:
        """Look up a connection by ID.

        Args:
            connection_id: The ``Acp-Connection-Id`` value.

        Returns:
            The :class:`Connection` or ``None``.
        """
        return self._connections.get(connection_id)

    async def remove(self, connection_id: str) -> Connection | None:
        """Remove and shut down a connection.

        Args:
            connection_id: The ``Acp-Connection-Id`` to remove.

        Returns:
            The removed :class:`Connection`, or ``None`` if not found.
        """
        conn = self._connections.pop(connection_id, None)
        if conn is not None:
            await conn.wait_closed()
            logger.info(
                "Connection %s removed (total=%d)",
                connection_id,
                len(self._connections),
            )
        return conn

    async def cleanup_expired(self) -> int:
        """Remove connections idle longer than :attr:`_idle_timeout`.

        Returns:
            Number of connections cleaned up.
        """
        if self._idle_timeout <= 0:
            return 0
        now = time.monotonic()
        expired = [
            cid
            for cid, conn in self._connections.items()
            if now - conn.created_at > self._idle_timeout
        ]
        for cid in expired:
            await self.remove(cid)
        if expired:
            logger.info("Cleaned up %d expired connections", len(expired))
        return len(expired)

    def lookup_by_cookie(self, cookie_value: str) -> Connection | None:
        """Look up a connection by cookie value (stored as connection_id).

        Used for SSE GET requests where the browser cannot send custom
        headers but can send cookies.

        Args:
            cookie_value: The value of the ``acp_conn`` cookie.

        Returns:
            The :class:`Connection` or ``None``.
        """
        return self._connections.get(cookie_value)

    @property
    def count(self) -> int:
        """Number of active connections."""
        return len(self._connections)
