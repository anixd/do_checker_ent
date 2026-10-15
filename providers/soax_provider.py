"""
SOAX behind the provider seam (providers/base.py).

Everything SOAX-specific that the engine used to do inline lives here: the
connection string, the preflight against SOAX's own IP lookup, and the fact that
a gateway refusal is `proxy_error` rather than a property of the checked site.
engine/worker.py now goes through providers/registry.py and names no provider.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Iterator

import requests

from config.loader import ConfigStore
from engine.classify import classify_transport
from logging_.engine_logger import get_engine_logger

from .base import ProviderGatewayError, ScreenshotProxy
from .socks_shim import local_socks5_shim
from .soax_session import ProxySession, get_session, sanitize_credentials, SoaxGatewayError

log = get_engine_logger()

# Plain HTTP on purpose. Over HTTPS the proxy answers during CONNECT and requests
# raises ProxyError without exposing the response headers, so the X-SOAX-*
# diagnostics would be lost. Over HTTP the 301 to HTTPS only happens once the
# proxy has already accepted the request.
PREFLIGHT_URL = "http://checker.soax.com/api/ipinfo"
PREFLIGHT_TIMEOUT_SEC = 20


def _soax_error(resp: requests.Response) -> tuple[str | None, str | None]:
    """Extracts the gateway's own error headers, if it set any."""
    return resp.headers.get("X-SOAX-Error"), resp.headers.get("X-SOAX-Detail")


def raise_for_gateway_error(resp: requests.Response) -> None:
    """
    Turns a gateway refusal carried in response headers into an exception.

    Called by the engine on the first hop of the real check too: the gateway
    answers with a normal HTTP response whose headers carry the diagnosis.
    """
    code, detail = _soax_error(resp)
    if code:
        raise SoaxGatewayError(code, detail, resp.status_code)


class SoaxProvider:
    name = "soax"

    def build_session(self, task: Any, options: Any) -> ProxySession:
        """
        task carries country and session_id; options carries the network type.
        The rule string is built once per session and must not vary between the
        preflight and the check itself - the gateway locks a session's rules on
        its first request and answers 409 SESSION_PARAMS_MISMATCH otherwise.
        """
        return get_session({
            "country": task.country,
            "network": options.network,
            "session_id": task.session_id,
            "transport": options.channel.transport,
        })

    def proxies_for(self, session: ProxySession, channel: Any = None) -> dict:
        """
        requests-style mapping for the run's transport.

        channel=None means "the control plane", i.e. the preflight: it always
        goes over http, because that is the only transport on which the gateway
        can tell us *why* it refused (over SOCKS5 a bad rule and a bad password
        are the same nameless connection failure).
        """
        scheme = channel.proxy_scheme if channel is not None else "http"
        proxy_url = session.proxy_url(scheme)
        return {"http": proxy_url, "https": proxy_url}

    @contextlib.contextmanager
    def screenshot_proxy(self, session: ProxySession, channel: Any) -> Iterator[ScreenshotProxy]:
        """
        Proxy settings for the screenshot browser, for the duration of the shot.

        For an http run, the gateway directly - Chromium authenticates to an
        HTTP proxy without complaint.

        For a socks5 run, a local shim: Playwright refuses socks5 with
        credentials, and the gateway refuses to target without them, so the shim
        takes the credentials and Chromium talks to 127.0.0.1 unauthenticated.
        Same session id either way, so the same exit node as the check.

        If the shim cannot start, the screenshot falls back to http rather than
        being lost, and the returned transport says so for the card.
        """
        if channel is None or channel.transport != "socks5":
            yield ScreenshotProxy(self._http_proxy_settings(session), "http")
            return

        try:
            with local_socks5_shim(
                upstream_host=session.host,
                upstream_port=session.port,
                username=session.username,
                password=session.password,
                remote_dns=(channel.resolve == "remote"),
            ) as port:
                yield ScreenshotProxy(
                    {"server": f"socks5://127.0.0.1:{port}"},  # no credentials: that is the point
                    "socks5",
                )
            return
        except OSError as e:
            log.warning("Could not start the SOCKS5 shim (%s); screenshot falls back to http", e)

        yield ScreenshotProxy(self._http_proxy_settings(session), "http")

    def _http_proxy_settings(self, session: ProxySession) -> dict:
        return {
            "server": f"http://{session.host}:{session.port}",
            "username": session.username,
            "password": session.password,
        }

    def preflight(self, session: ProxySession) -> str | None:
        """
        Opens the session and records which exit node it landed on.

        Populates session.ext_ip and session.geo_actual. Raises SoaxGatewayError
        when the gateway refuses the request - there is no point checking sites
        through a proxy that will refuse every one of them.

        Returns a warning string when the lookup itself failed but the proxy is
        fine (checker.soax.com being down is not our problem), or None on success.
        Returns None without doing anything when preflight is switched off.
        """
        if not ConfigStore.get().soax.preflight:
            return None

        proxies = self.proxies_for(session, None)
        try:
            resp = requests.get(
                PREFLIGHT_URL,
                proxies=proxies,
                timeout=PREFLIGHT_TIMEOUT_SEC,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as e:
            return f"preflight failed: {sanitize_credentials(str(e))}"

        raise_for_gateway_error(resp)

        if resp.status_code != 200:
            return f"preflight returned HTTP {resp.status_code}"

        try:
            data = resp.json().get("data") or {}
        except (ValueError, json.JSONDecodeError):
            return "preflight returned a non-JSON body"

        session.ext_ip = data.get("ip") or None
        session.geo_actual = {
            "country_code": (data.get("country_code") or "").lower(),
            "region": data.get("region") or "",
            "city": data.get("city") or "",
            "isp": data.get("isp") or data.get("aso") or "",
            "asn": data.get("asn"),
        }

        if not session.ext_ip:
            return "preflight response carried no IP"

        # A wrong exit country makes the whole check meaningless, so surface it
        # rather than quietly reporting "available in az" from a node in Germany.
        actual_cc = session.geo_actual["country_code"]
        if actual_cc and actual_cc != session.country:
            return f"geo mismatch: requested {session.country}, exit node is in {actual_cc}"

        return None

    def check_response(self, resp: requests.Response) -> None:
        """
        Lets the provider veto a response that is really its own refusal.

        The first hop of a check is plain HTTP, so a gateway rejection arrives as
        an ordinary response with X-SOAX-* headers intact rather than as a
        transport error.
        """
        raise_for_gateway_error(resp)

    def classify(self, exc: Exception | None, http_status: int | None) -> str:
        # Checked first: a gateway refusal is a configuration or quota problem,
        # not a property of the site being checked.
        if isinstance(exc, ProviderGatewayError):
            return "proxy_error"
        return classify_transport(exc, http_status)
