"""
The seam a provider has to fit through.

Deliberately three operations wide, no more. Everything the engine needs from a
proxy service is: build me a session for this task, tell me where it came out,
and tell me whether a failure was yours or the target site's. The rest of a
check - timings, redirects, screenshots, the Markdown card - is transport work
and belongs to the engine.

This is not a general abstraction over proxy providers; it is the smallest shape
that lets fastproxys.net land later without reshaping the engine, written while
there is still only one implementation. Widening it is cheap once there are two
to compare.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ProviderGatewayError(RuntimeError):
    """
    The proxy service itself refused the request, as opposed to the target site
    answering with an error.

    The engine classifies this as `proxy_error` - a configuration or quota
    problem on our side, not a property of the site being checked - so every
    provider must raise this type (or a subclass) for its own refusals.
    """

    def __init__(self, code: str, detail: str | None = None, http_status: int | None = None):
        self.code = code
        self.detail = detail
        self.http_status = http_status
        message = f"{code}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


@runtime_checkable
class Provider(Protocol):
    """What engine/worker.py calls. See providers/soax_provider.py for the one implementation."""

    name: str

    def build_session(self, task: Any, channel: Any) -> Any:
        """Turns a task into a ready-to-use session (credentials, host, port, rules)."""

    def preflight(self, session: Any) -> str | None:
        """
        Resolves the exit IP and real geo into the session before the real check.

        Returns a human-readable warning when the lookup failed but the proxy is
        usable, None when there is nothing to report, and raises
        ProviderGatewayError when the service refuses the request outright.
        """

    def check_response(self, resp: Any) -> None:
        """Raises ProviderGatewayError if this response is the provider's own refusal."""

    def classify(self, exc: Exception | None, http_status: int | None) -> str:
        """Maps an outcome onto the engine's result classes."""

    def proxies_for(self, session: Any, channel: Any) -> dict:
        """requests-style proxy mapping for this session and channel."""
