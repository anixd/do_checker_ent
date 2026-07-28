"""
Builds credentials for the SOAX gateway (proxy.soax.com:1337).

The gateway encodes targeting and behaviour rules inside the proxy *username*,
and takes the package password as the proxy *password*:

    country-az-network-res_mob-onerror-retry_3-session-dca1b2c3d407 : <password>

Rule order follows the dashboard's own generator. The docs claim rules may
appear in any order, but matching the dashboard costs nothing and avoids
relying on undocumented parser behaviour.

This module deliberately imports nothing from the rest of the project, so
build_username() can be unit tested without config, logging or a filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict

# Verified against the gateway on 2026-07-28.
ALLOWED_NETWORKS = ("res", "mob", "res_mob", "any")
ALLOWED_ON_ERROR = ("replace", "retry", "fail")

# The gateway accepts [A-Za-z0-9_] up to 32 chars, but the dashboard's own
# Session ID field refuses anything but lowercase alphanumerics up to 16.
# We stick to the stricter form so any session ID from our logs can be pasted
# into the dashboard to reproduce a check by hand.
_SESSION_RE = re.compile(r"^[a-z0-9]{1,16}$")
_COUNTRY_RE = re.compile(r"^[a-z]{2}$")

# Matches credentials inside a proxy URL: scheme://user:pass@host:port
_CREDS_RE = re.compile(r"(?P<scheme>[a-zA-Z0-9+.-]+://)[^@/\s]+@")


class SoaxConfigError(ValueError):
    """Raised when the requested rules cannot produce a valid username."""


def sanitize_credentials(text: str) -> str:
    """
    Strips user:pass out of any proxy URL embedded in a string.

    requests puts the full proxy URL into its exception messages, and those
    messages end up in check notes, markdown logs and the UI. Without this,
    the package password would be written to ./logs in plain text.
    """
    if not text:
        return text
    return _CREDS_RE.sub(lambda m: m.group("scheme") + "***@", str(text))


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Renders a secret as 'abcd...wxyz' for debug output."""
    if not value:
        return "(not set)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def make_session_id(run_id: str, index: int) -> str:
    """
    Builds a session ID of the form dc<run><nn>, e.g. dca1b2c3d407.

    Only run_id and a counter go in — never a domain, not even hashed. Keeps
    the ID inside the dashboard's 16-char lowercase-alphanumeric limit and
    avoids leaking what is being checked into SOAX-side logs.
    """
    base = re.sub(r"[^a-z0-9]", "", str(run_id).lower())[:8]
    if not base:
        raise SoaxConfigError(f"run_id yields no usable characters: {run_id!r}")
    session_id = f"dc{base}{index:02d}"
    if not _SESSION_RE.match(session_id):
        raise SoaxConfigError(
            f"generated session ID {session_id!r} is not [a-z0-9]{{1,16}}"
        )
    return session_id


def build_username(
        country: str,
        network: str,
        session_id: str | None = None,
        on_error: str = "replace",
        on_error_retries: int = 3,
        bind_node: bool = False,
) -> str:
    """
    Assembles the rule string used as the proxy username.

    Rotation rules (rotate-timed_N / rotate-requests_N) are intentionally not
    supported. rotate-requests would swap the IP mid-page-load, since a single
    Chromium render issues dozens of proxied requests; rotate-timed would swap
    it mid-run. Both break the premise that one check equals one IP.
    """
    country = (country or "").strip().lower()
    network = (network or "").strip().lower()
    on_error = (on_error or "").strip().lower()

    # The gateway happily answers 200 with no country rule at all, routing
    # through an arbitrary country. A dropped country would therefore produce a
    # plausible-looking but meaningless result, so validate it here.
    if not _COUNTRY_RE.match(country):
        raise SoaxConfigError(
            f"country must be a lowercase ISO 3166-1 alpha-2 code, got {country!r}"
        )

    if network not in ALLOWED_NETWORKS:
        raise SoaxConfigError(
            f"network must be one of {'|'.join(ALLOWED_NETWORKS)}, got {network!r}"
        )

    if on_error not in ALLOWED_ON_ERROR:
        raise SoaxConfigError(
            f"on_error must be one of {'|'.join(ALLOWED_ON_ERROR)}, got {on_error!r}"
        )

    if on_error == "retry" and not (1 <= int(on_error_retries) <= 10):
        raise SoaxConfigError(
            f"on_error_retries must be between 1 and 10, got {on_error_retries}"
        )

    # The gateway rejects this with 400 ERR_INVALID_COMBINATION; the dashboard
    # greys the option out. Catch it before spending a request on it.
    if bind_node and on_error == "replace":
        raise SoaxConfigError(
            "bind_node cannot be combined with on_error='replace' "
            "(gateway returns 400 ERR_INVALID_COMBINATION)"
        )

    if session_id is not None and not _SESSION_RE.match(session_id):
        raise SoaxConfigError(
            f"session ID must match [a-z0-9]{{1,16}}, got {session_id!r}"
        )

    if bind_node and session_id is None:
        raise SoaxConfigError("bind_node requires a session ID")

    parts = [f"country-{country}", f"network-{network}"]

    # 'replace' is the gateway default and adds no token, matching the
    # dashboard: picking "Rotate" under Error Handling emits nothing.
    if on_error == "retry":
        parts.append(f"onerror-retry_{int(on_error_retries)}")
    elif on_error == "fail":
        parts.append("onerror-fail")

    if bind_node:
        parts.append("bind-node")

    if session_id:
        parts.append(f"session-{session_id}")

    return "-".join(parts)


@dataclass
class ProxySession:
    type: str
    host: str
    port: int
    username: str
    password: str
    session_id: str | None
    country: str
    network: str
    ext_ip: str | None = None
    geo_actual: Dict[str, Any] | None = None
    debug_info: Dict[str, Any] = field(default_factory=dict)

    def proxy_url(self) -> str:
        """Full proxy URL. Never log this - use safe_repr() instead."""
        return f"{self.type}://{self.username}:{self.password}@{self.host}:{self.port}"

    def safe_repr(self) -> str:
        """Human-readable identity of the session with no secrets in it."""
        return f"{self.type}://{self.username}@{self.host}:{self.port}"


def get_session(params: dict[str, Any]) -> ProxySession:
    """
    Turns task parameters into a ready-to-use ProxySession.

    Expected in params:
        country     - ISO 3166-1 alpha-2, lowercase
        network     - res | mob | res_mob | any
        session_id  - produced by make_session_id() in the orchestrator

    Everything else (host, port, protocol, error handling) comes from config.
    """
    # Imported here rather than at module level so that build_username stays
    # testable without touching the config machinery.
    from config.loader import ConfigStore

    cfg = ConfigStore.get().soax

    if not cfg.package_password:
        raise SoaxConfigError(
            "SOAX_PACKAGE_PASSWORD is not set - cannot authenticate to the gateway"
        )

    country = (params.get("country") or "").strip().lower()
    network = (params.get("network") or "").strip().lower()
    session_id = params.get("session_id")

    username = build_username(
        country=country,
        network=network,
        session_id=session_id,
        on_error=cfg.on_error,
        on_error_retries=cfg.on_error_retries,
        bind_node=cfg.bind_node,
    )

    debug_data = {
        "mode": "Session-mode (gateway)",
        "username": username,
        "password": mask_secret(cfg.package_password),
        "host": cfg.host,
        "port": cfg.port,
        "session_scope": cfg.session_scope,
        "source_params": {
            "country": country,
            "network": network,
            "session_id": session_id,
        },
    }

    return ProxySession(
        type=cfg.proxy_type,
        host=cfg.host,
        port=cfg.port,
        username=username,
        password=cfg.package_password,
        session_id=session_id,
        country=country,
        network=network,
        ext_ip=None,
        geo_actual=None,
        debug_info=debug_data,
    )
