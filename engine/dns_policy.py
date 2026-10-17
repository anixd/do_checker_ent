"""
Process-wide IPv6 policy.

SOAX does not route IPv6: an IPv6-only host is unreachable through the gateway
both by name and by explicit address, and the gateway answers "0x03: Network
unreachable". So every outbound attempt over IPv6 is a guaranteed failure, and in a mirror report a guaranteed failure reads as "the site is down":
a false verdict, which is worse than no check at all.

Hence a single switch (`dns.use_ipv6`, off by default) applied where the choice
is actually made - the resolve. With IPv6 off, AAAA records are dropped from
every answer `socket.getaddrinfo` gives this process, which covers all three
consumers at once instead of three separate flags:

* requests/urllib3 - its create_connection() calls socket.getaddrinfo and walks
  the list it gets;
* PySocks - both when it resolves the gateway address and, in `local` resolve
  mode, when it resolves the target before writing the CONNECT request. Left to
  itself it takes the first entry ("We can't really work out what IP is
  reachable, so just pick the first"), which on a dual-stack host is the AAAA;
* providers/socks_shim.py - its own local resolve, plus the IPv6 literal that a
  browser may put in CONNECT (refused there, see _read_target).

Deliberately NOT covered:

* the checked site's own DNS. At `resolve=remote` the exit node resolves the
  name, and mirrors do have geo-dependent DNS - a local "no IPv4 address" would
  be a false verdict about a site that answers perfectly well from the country
  being checked. That verdict stays with the gateway.
* gevent's internal resolver. `gevent._socketcommon` keeps its own binding of
  getaddrinfo, reached only by a raw `sock.connect(("name", port))`; nothing in
  this project does that - PySocks resolves the gateway through
  socket.getaddrinfo first. Patching a private gevent binding would buy
  coverage of the gateway hostname (IPv4-only in any case) at the price of
  breaking on a gevent rename.
* Chromium during a screenshot. It is a separate process and never resolves the
  target anyway: with a proxy configured it hands over the hostname.
"""

from __future__ import annotations

import socket

from logging_.engine_logger import get_engine_logger

log = get_engine_logger()

_installed = False
_use_ipv6 = True
_real_getaddrinfo = None
_real_has_ipv6 = socket.has_ipv6


def ipv6_enabled() -> bool:
    """
    Whether IPv6 is allowed in this process.

    Defaults to True until apply_ipv6_policy() has run, so a module imported by
    a unit test or a script without a config behaves exactly as it did before
    this switch existed.
    """
    return _use_ipv6


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """
    socket.getaddrinfo with the AAAA records taken out.

    The failure mode matters as much as the filtering: an empty list would turn
    into "getaddrinfo returns an empty list" several layers up, so a name that
    resolves to IPv6 only raises gaierror with a message naming the switch.
    The message deliberately contains "dns" - engine/classify.py matches on the
    exception text, and this is a resolve failure, so it has to land on
    dns_error rather than on the connect_error catch-all.
    """
    if family == socket.AF_INET6:
        # An explicit AAAA lookup. Answering honestly beats returning an empty
        # list the caller is not expecting.
        raise socket.gaierror(
            socket.EAI_NONAME,
            f"IPv6 is off (dns.use_ipv6=false), no AAAA lookup for {host!r}",
        )

    infos = _real_getaddrinfo(host, port, family, type, proto, flags)
    if family != 0:
        # AF_INET (or anything else explicit) - nothing of ours to strip.
        return infos

    filtered = [info for info in infos if info[0] != socket.AF_INET6]
    if filtered:
        return filtered
    if infos:
        raise socket.gaierror(
            socket.EAI_NONAME,
            f"{host!r} resolves to IPv6 only and IPv6 is off (dns.use_ipv6=false)",
        )
    return infos


def apply_ipv6_policy(use_ipv6: bool) -> None:
    """
    Installs or removes the filter, and says so in the log.

    Called once per worker from app/__init__.py::create_app, after the loggers
    exist. Idempotent, and symmetric: apply_ipv6_policy(True) puts the original
    socket.getaddrinfo back, which is what lets the unit tests switch it on and
    off around a fake resolver.

    socket.has_ipv6 is lowered too. Nothing in this project reads it, but
    third-party code uses it to decide whether to even try an AF_INET6 socket,
    and a library that asks deserves the honest answer for this process.
    """
    global _installed, _use_ipv6, _real_getaddrinfo, _real_has_ipv6

    _use_ipv6 = bool(use_ipv6)

    if _use_ipv6:
        if _installed:
            socket.getaddrinfo = _real_getaddrinfo
            socket.has_ipv6 = _real_has_ipv6
            _installed = False
        log.info(
            "IPv6 enabled (dns.use_ipv6=true): resolves may return AAAA, "
            "and the SOAX gateway does not route IPv6."
        )
        return

    if not _installed:
        # Captured here rather than at import time: gevent's monkey.patch_all()
        # has replaced socket.getaddrinfo by now, and that is the one to wrap.
        _real_getaddrinfo = socket.getaddrinfo
        _real_has_ipv6 = socket.has_ipv6
        socket.getaddrinfo = _ipv4_only_getaddrinfo
        socket.has_ipv6 = False
        _installed = True

    log.info(
        "IPv6 disabled (dns.use_ipv6=false): AAAA records are dropped from "
        "every resolve in this process."
    )
