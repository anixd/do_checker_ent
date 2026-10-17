"""
The seam a provider has to fit through.

Deliberately narrow. Everything the engine needs from a proxy service is: build
me a session for this task, tell me where it came out, tell me whether you are
reachable at all, and tell me whether a failure was yours or the target site's.
The rest of a check - timings, redirects, screenshots, the Markdown card - is
transport work and belongs to the engine.

This is not a general abstraction over proxy providers; it is the smallest shape
that lets a second one land without reshaping the engine. Widening it is cheap
once there are two implementations to compare.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
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


@dataclass
class ScreenshotProxy:
    """
    What the screenshot browser should use, and which transport that turned out
    to be.

    The two can differ: a SOCKS5 run normally gets a shim and therefore socks5,
    but if the shim cannot start we fall back to http rather than lose the
    picture - and then the check's card has to say so, because a screenshot
    taken over a different transport is not evidence about this one.
    """

    settings: dict
    transport: str


@dataclass
class AliveProbe:
    """
    One request of the pre-run reachability check.

    Kept raw on purpose: the provider reports what happened to each target and
    the engine decides what that means (engine/alive.py owns the "two of three"
    policy). A provider that needs a different notion of "responded" overrides
    `ok`, not the threshold.
    """

    target: str
    ok: bool
    status: int | None = None
    ms: int = 0
    error: str | None = None


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

    def check_alive(self, task: Any, options: Any, targets: list[str],
                    timeout_sec: int, stop_after_ok: int = 0) -> list[AliveProbe]:
        """
        Probes a handful of third-party sites through the proxy and reports
        each outcome.

        Takes a task and the run options rather than a ready session, because
        how many sessions this needs is the provider's business: SOAX gives
        each probe its own, so that one target failing cannot drag another
        probe down with it (see providers/soax_provider.py).

        Must not raise for an unreachable target or a refusing gateway - those
        are results, and a dead provider is exactly what the caller is asking
        about. The caller decides how many successes are enough; stop_after_ok
        tells the provider it may stop once that many have answered, so a
        healthy proxy is not held up by one slow target. Fewer probes than
        targets in the returned list means it stopped early.
        """

    def check_response(self, resp: Any) -> None:
        """Raises ProviderGatewayError if this response is the provider's own refusal."""

    def classify(self, exc: Exception | None, http_status: int | None) -> str:
        """Maps an outcome onto the engine's result classes."""

    def proxies_for(self, session: Any, channel: Any = None) -> dict:
        """
        requests-style proxy mapping for this session and channel.

        channel=None means the control plane (preflight), which may use a
        different transport than the check itself.
        """

    def screenshot_proxy(self, session: Any, channel: Any) -> AbstractContextManager:
        """
        Context manager yielding a ScreenshotProxy for the duration of a
        screenshot. Anything it has to start (a local shim, say) lives only
        inside the block.
        """
