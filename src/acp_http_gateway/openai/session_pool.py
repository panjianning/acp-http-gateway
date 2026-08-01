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
"""LRU pool of ACP connections/sessions for the OpenAI compatibility layer.

Each OpenAI session maps 1:1 to an ACP session, which lives inside an
agent subprocess (1 connection = 1 process = 1 active session).

The pool keeps ``session_id → PooledSession`` entries.  When the pool is
full or an entry is idle too long, the connection is torn down
(``DELETE`` equivalent) and evicted.  If a client returns with a
``session_id`` that was evicted, the caller should attempt
``session/load`` to restore the conversation from disk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..connection import Connection

logger = logging.getLogger(__name__)


class PooledSession:
    """A pooled ACP connection bound to one ACP session."""

    def __init__(
        self,
        connection: Connection,
        acp_session_id: str,
        startup_info: str | None = None,
    ) -> None:
        """Initialize a pooled session.

        Args:
            connection: The agent connection (subprocess).
            acp_session_id: The ACP session id within that connection.
            startup_info: The agent's prelude banner text, if any.  Used
                to filter the banner out of the first assistant reply.
        """
        self.connection: Connection = connection
        self.acp_session_id: str = acp_session_id
        self.startup_info: str | None = startup_info
        self.last_used: float = time.monotonic()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lock: asyncio.Lock = asyncio.Lock()

    def touch(self) -> None:
        """Update the last-used timestamp."""
        self.last_used = time.monotonic()


class SessionPool:
    """LRU pool mapping OpenAI session ids to ACP connections.

    Thread-safe within the single asyncio event loop.
    """

    def __init__(self, max_size: int = 20, idle_timeout: float = 600.0) -> None:
        """Initialize the pool.

        Args:
            max_size: Maximum number of concurrent pooled connections.
            idle_timeout: Seconds of inactivity before eviction (0 = never).
        """
        self._sessions: dict[str, PooledSession] = {}
        self._max_size = max_size
        self._idle_timeout = idle_timeout

    def get(self, session_id: str) -> PooledSession | None:
        """Look up a pooled session and touch it.

        Args:
            session_id: The OpenAI session id.

        Returns:
            The :class:`PooledSession` or ``None``.
        """
        pooled = self._sessions.get(session_id)
        if pooled is not None:
            pooled.touch()
        return pooled

    def put(self, session_id: str, pooled: PooledSession) -> None:
        """Insert or refresh an entry.

        If the pool is at capacity, evicts the least-recently-used entry.

        Args:
            session_id: The OpenAI session id.
            pooled: The pooled session to store.
        """
        self._sessions[session_id] = pooled
        if len(self._sessions) > self._max_size:
            self._evict_lru()

    def _evict_lru(self) -> PooledSession | None:
        """Evict the least-recently-used entry.

        Returns:
            The evicted :class:`PooledSession`, or ``None`` if empty.
        """
        if not self._sessions:
            return None
        lru_id = min(self._sessions, key=lambda k: self._sessions[k].last_used)
        pooled = self._sessions.pop(lru_id)
        logger.info("SessionPool evicted LRU session %s", lru_id)
        return pooled

    async def evict(self, session_id: str) -> PooledSession | None:
        """Remove and shut down a specific session.

        Args:
            session_id: The session to evict.

        Returns:
            The evicted :class:`PooledSession`, or ``None`` if absent.
        """
        pooled = self._sessions.pop(session_id, None)
        if pooled is not None:
            await pooled.connection.wait_closed()
            logger.info("SessionPool evicted session %s", session_id)
        return pooled

    async def evict_all(self) -> None:
        """Shut down all pooled sessions (server shutdown)."""
        for session_id in list(self._sessions):
            await self.evict(session_id)

    async def cleanup_expired(self) -> int:
        """Evict sessions idle longer than the timeout.

        Returns:
            Number of sessions evicted.
        """
        if self._idle_timeout <= 0:
            return 0
        now = time.monotonic()
        expired = [
            sid
            for sid, pooled in self._sessions.items()
            if now - pooled.last_used > self._idle_timeout
        ]
        for sid in expired:
            await self.evict(sid)
        if expired:
            logger.info("SessionPool cleaned up %d idle sessions", len(expired))
        return len(expired)

    @property
    def count(self) -> int:
        """Number of pooled sessions."""
        return len(self._sessions)
