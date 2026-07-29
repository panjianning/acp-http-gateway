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
"""Authentication abstraction for the ACP HTTP gateway.

Provides a pluggable auth interface so deployments can integrate with
their own identity providers (OAuth, API keys, mTLS, etc.).

Auth happens **once** during the ``initialize`` POST request.  After
successful authentication, the gateway issues an ``Acp-Connection-Id``
that identifies the connection for all subsequent requests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from aiohttp import web


class AuthValidator(ABC):
    """Pluggable authentication validator.

    Subclass and pass to :func:`acp_http_gateway.run_server` to enforce
    authentication on the ``initialize`` POST request.

    Example::

        class OAuthValidator(AuthValidator):
            async def validate(self, request: web.Request) -> dict | None:
                token = request.headers.get("Authorization", "").removeprefix("Bearer ")
                user = await self._verify_id_token(token)
                return {"sub": user.sub, "email": user.email} if user else None

    Returns:
        A ``dict`` with auth context (attached to the Connection) on success,
        or ``None`` to reject with 401.
    """

    @abstractmethod
    async def validate(self, request: web.Request) -> dict[str, Any] | None:
        """Validate an incoming request.

        Args:
            request: The aiohttp request for the ``initialize`` POST.

        Returns:
            Auth context dict on success, ``None`` on failure.
        """
        ...


class NoAuthValidator(AuthValidator):
    """No-op validator — allows all connections.

    Suitable for local development, trusted networks, or when
    authentication is handled by a reverse proxy.
    """

    async def validate(self, request: web.Request) -> dict[str, Any]:
        """Always succeeds with an empty context."""
        return {}


class BearerTokenValidator(AuthValidator):
    """Validate a fixed bearer token from the ``Authorization`` header.

    Intended for development and simple deployments.  For production,
    use a custom :class:`AuthValidator` backed by your identity provider.

    Example::

        BearerTokenValidator("sk-secret-token")
    """

    def __init__(self, token: str, *, header_name: str = "Authorization") -> None:
        """Initialize with the expected token.

        Args:
            token: The expected bearer token value (without ``Bearer `` prefix).
            header_name: The HTTP header to inspect.  Defaults to
                ``Authorization``.
        """
        self._token = token
        self._header_name = header_name.lower()
        self._expected = f"Bearer {token}"

    async def validate(self, request: web.Request) -> dict[str, Any] | None:
        """Validate the bearer token.

        Returns:
            ``{"auth_method": "bearer_token"}`` on success, ``None`` on failure.
        """
        value = request.headers.get(self._header_name, "")
        if value == self._expected:
            return {"auth_method": "bearer_token"}
        return None
