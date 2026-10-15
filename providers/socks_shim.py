"""
A local SOCKS5 front end for a proxy that requires authentication.

Why this exists: Playwright refuses `socks5://` together with a username and
password - `normalizeProxySettings()` throws "Browser does not support socks5
proxy authentication" before any browser starts, for Chromium and Firefox
alike - while SOAX carries no targeting rules without authentication. So a
screenshot during a SOCKS5 run had nowhere to go.

The shim closes that gap: it listens on loopback with no authentication at all,
and every connection it accepts is forwarded through the real proxy with the
credentials attached. The browser is pointed at 127.0.0.1 and never sees them;
they never reach a command line or a file either, which is the reason this is
~200 lines of our own rather than a gost or 3proxy process (решение 13).

Scope is deliberately the narrow subset a browser needs: no authentication
methods, CONNECT only, IPv4/IPv6/domain targets. BIND and UDP ASSOCIATE are
refused rather than half-implemented.
"""

from __future__ import annotations

import contextlib
import socket
import struct
from typing import Iterator

import gevent
import socks
from gevent.server import StreamServer

from logging_.engine_logger import get_engine_logger

log = get_engine_logger()

SOCKS_VERSION = 0x05
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

# SOCKS5 reply codes (RFC 1928 §6)
REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_HOST_UNREACHABLE = 0x04
REP_CONNECTION_REFUSED = 0x05
REP_CMD_NOT_SUPPORTED = 0x07
REP_ATYP_NOT_SUPPORTED = 0x08

RELAY_CHUNK = 65536
UPSTREAM_CONNECT_TIMEOUT_SEC = 30


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """
    recv() may return less than asked for, and a handshake read short is a bug
    that shows up as a hang rather than an error.
    """
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(f"connection closed after {count - remaining} of {count} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _reply(sock: socket.socket, code: int) -> None:
    """A reply with a zeroed BND.ADDR: a CONNECT client has no use for it."""
    sock.sendall(struct.pack("!BBBB", SOCKS_VERSION, code, 0x00, ATYP_IPV4) + b"\x00" * 6)


def _read_target(sock: socket.socket) -> tuple[str, int]:
    """Reads the CONNECT request and returns (host, port). Raises on anything else."""
    header = _recv_exact(sock, 4)
    version, command, _reserved, address_type = header

    if version != SOCKS_VERSION:
        _reply(sock, REP_GENERAL_FAILURE)
        raise ValueError(f"unexpected SOCKS version {version}")
    if command != CMD_CONNECT:
        # BIND and UDP ASSOCIATE: a browser never asks, so refuse clearly.
        _reply(sock, REP_CMD_NOT_SUPPORTED)
        raise ValueError(f"unsupported command {command}")

    if address_type == ATYP_IPV4:
        host = socket.inet_ntoa(_recv_exact(sock, 4))
    elif address_type == ATYP_IPV6:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    elif address_type == ATYP_DOMAIN:
        length = _recv_exact(sock, 1)[0]
        host = _recv_exact(sock, length).decode("idna")
    else:
        _reply(sock, REP_ATYP_NOT_SUPPORTED)
        raise ValueError(f"unsupported address type {address_type}")

    port = struct.unpack("!H", _recv_exact(sock, 2))[0]
    return host, port


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(RELAY_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        # Either end closing mid-transfer is ordinary for a browser that has
        # finished with a connection.
        pass
    finally:
        for sock in (src, dst):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


class _ShimHandler:
    """Holds the upstream details so the handler stays a plain callable."""

    def __init__(self, host: str, port: int, username: str, password: str, rdns: bool):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.rdns = rdns

    def __call__(self, client: socket.socket, address) -> None:
        upstream = None
        try:
            self._negotiate_no_auth(client)
            target_host, target_port = _read_target(client)
            upstream = self._connect_upstream(client, target_host, target_port)
            if upstream is None:
                return

            _reply(client, REP_SUCCESS)
            pump = gevent.spawn(_relay, client, upstream)
            _relay(upstream, client)
            pump.join(timeout=5)
        except (ConnectionError, ValueError) as e:
            # Credentials never appear in these messages: the only values here
            # are the client's own request and our upstream host.
            log.debug("Shim connection dropped: %s", e)
        except Exception as e:
            log.warning("Shim connection failed: %s", e, exc_info=True)
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.close()

    def _negotiate_no_auth(self, client: socket.socket) -> None:
        version, method_count = _recv_exact(client, 2)
        if version != SOCKS_VERSION:
            raise ValueError(f"unexpected SOCKS version {version}")
        _recv_exact(client, method_count)  # whatever the client offers, we want none
        client.sendall(struct.pack("!BB", SOCKS_VERSION, 0x00))

    def _connect_upstream(self, client: socket.socket, host: str, port: int):
        """
        Opens the authenticated upstream connection, or answers the client with
        the matching SOCKS5 failure code and returns None.
        """
        upstream = socks.socksocket()
        upstream.set_proxy(
            socks.SOCKS5, self.host, self.port,
            rdns=self.rdns, username=self.username, password=self.password,
        )
        upstream.settimeout(UPSTREAM_CONNECT_TIMEOUT_SEC)
        try:
            upstream.connect((host, port))
        except socks.ProxyConnectionError as e:
            # The proxy itself is unreachable - wrong host or port, or it is down.
            log.warning("Shim could not reach the upstream proxy: %s", e)
            _reply(client, REP_GENERAL_FAILURE)
        except socks.SOCKS5AuthError as e:
            log.error("Shim upstream rejected our credentials: %s", e)
            _reply(client, REP_GENERAL_FAILURE)
        except socks.ProxyError as e:
            # Where a gateway refusal lands. PySocks wraps it as
            # GeneralProxyError("Socket error: 0x05: Connection refused") rather
            # than SOCKS5Error, so catch the base class - verified against a
            # refusing upstream. Over SOCKS5 the refusal carries no code or
            # detail of its own, which is why the HTTP preflight stays the
            # source of diagnosis for a run (решение 4).
            # Debug, not warning: one page render opens dozens of connections
            # and the gateway routinely refuses some of them - third-party
            # trackers and CDNs it will not route. Observed in a successful
            # screenshot: "0x03: Network unreachable" for r6.uuidksinc.net while
            # the page itself rendered fine. At warning level every screenshot
            # would bury the log in noise.
            log.debug("Shim upstream refused %s:%s - %s", host, port, e)
            _reply(client, REP_CONNECTION_REFUSED)
        except OSError as e:
            log.debug("Shim upstream connect to %s:%s failed: %s", host, port, e)
            _reply(client, REP_HOST_UNREACHABLE)
        else:
            upstream.settimeout(None)
            return upstream

        with contextlib.suppress(OSError):
            upstream.close()
        return None


@contextlib.contextmanager
def local_socks5_shim(
    upstream_host: str,
    upstream_port: int,
    username: str,
    password: str,
    remote_dns: bool = True,
) -> Iterator[int]:
    """
    Runs the shim for the duration of the block and yields its local port.

    Bound to 127.0.0.1 on an ephemeral port: the listener takes no credentials,
    so anything able to reach it would be an open proxy, and inside a container
    network that is not theoretical. One listener per screenshot, torn down with
    the block.

    remote_dns mirrors the run's DNS resolve mode - True hands the hostname to
    the exit node (socks5h), False resolves it here before connecting.
    """
    handler = _ShimHandler(upstream_host, upstream_port, username, password, remote_dns)
    server = StreamServer(("127.0.0.1", 0), handler)
    server.start()
    port = server.server_port
    log.debug("Shim started: 127.0.0.1:%s -> %s:%s (remote_dns=%s)",
              port, upstream_host, upstream_port, remote_dns)
    try:
        yield port
    finally:
        server.stop(timeout=5)
        log.debug("Shim stopped: 127.0.0.1:%s", port)
