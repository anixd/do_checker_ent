"""
Result classification that is not specific to any provider.

Lives apart from engine/worker.py so a provider can reuse it for everything
except its own refusals, which only it can recognise.
"""

from __future__ import annotations


def classify_transport(exc: Exception | None, http_status: int | None) -> str:
    """
    Maps a transport-level outcome onto the result classes the UI knows:
    success, http_error, dns_error, timeout, tls_error, connect_error.

    String matching on the exception text is crude, but requests wraps the real
    cause several layers deep and the message is the only thing that survives
    consistently across urllib3 versions.
    """
    if exc:
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text or "dns" in text:
            return "dns_error"
        if "timed out" in text or "timeout" in text:
            return "timeout"
        if "ssl" in text or "tls" in text:
            return "tls_error"
        return "connect_error"
    if http_status is None:
        return "connect_error"
    if 200 <= http_status < 400:
        return "success"
    return "http_error"
