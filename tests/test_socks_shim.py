"""
Unit tests for providers/socks_shim.py

No SOAX, no Docker, no network: a fake upstream SOCKS5 server is started inside
the test, the shim is pointed at it, and the protocol is spoken directly so the
assertions are about bytes rather than about a browser's behaviour.

    python -m unittest tests.test_socks_shim -v

gevent is monkey-patched here because that is how the application runs and the
shim's cooperative server and its blocking PySocks upstream have to coexist in
one hub - which is exactly the property worth testing.
"""

from gevent import monkey
monkey.patch_all()

import logging
import socket
import struct
import unittest

import gevent
from gevent.server import StreamServer

from providers.socks_shim import local_socks5_shim

# The shim logs refusals at warning level, which is the right thing in
# production and pure noise in a test asserting exactly those refusals.
_engine_log = logging.getLogger("engine")
_engine_log.addHandler(logging.NullHandler())
_engine_log.propagate = False

UPSTREAM_USER = "country-az-network-res_mob-session-dcabc00"
UPSTREAM_PASSWORD = "pk_test_password"
PAYLOAD = b"hello-through-two-proxies"


class FakeUpstream:
    """
    A SOCKS5 proxy that demands username/password and then connects onwards.

    Records the credentials it was given, so the test can prove the shim really
    attaches them rather than passing the browser's empty handshake through.
    """

    def __init__(self):
        self.seen_credentials: tuple[str, str] | None = None
        self.seen_atyp: int | None = None
        self.seen_host: str | None = None
        self.refuse = False
        self._server = StreamServer(("127.0.0.1", 0), self._handle)
        self._server.start()
        self.port = self._server.server_port

    def stop(self):
        self._server.stop(timeout=3)

    def _recv_exact(self, sock, count):
        chunks = b""
        while len(chunks) < count:
            chunk = sock.recv(count - len(chunks))
            if not chunk:
                raise ConnectionError("upstream client vanished")
            chunks += chunk
        return chunks

    def _handle(self, sock, _address):
        try:
            version, method_count = self._recv_exact(sock, 2)
            methods = self._recv_exact(sock, method_count)
            assert version == 5
            # 0x02 = username/password, which is what the shim must offer.
            if 0x02 not in methods:
                sock.sendall(b"\x05\xff")
                return
            sock.sendall(b"\x05\x02")

            self._recv_exact(sock, 1)  # auth subnegotiation version
            user_len = self._recv_exact(sock, 1)[0]
            user = self._recv_exact(sock, user_len).decode()
            pass_len = self._recv_exact(sock, 1)[0]
            password = self._recv_exact(sock, pass_len).decode()
            self.seen_credentials = (user, password)
            if (user, password) != (UPSTREAM_USER, UPSTREAM_PASSWORD):
                sock.sendall(b"\x01\x01")
                return
            sock.sendall(b"\x01\x00")

            header = self._recv_exact(sock, 4)
            atyp = header[3]
            if atyp == 0x01:
                host = socket.inet_ntoa(self._recv_exact(sock, 4))
            elif atyp == 0x04:
                # PySocks sends IPv6 when it resolves the name itself and the
                # name has an AAAA record - "localhost" does.
                host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(sock, 16))
            else:
                host = self._recv_exact(sock, self._recv_exact(sock, 1)[0]).decode()
            port = struct.unpack("!H", self._recv_exact(sock, 2))[0]
            self.seen_atyp, self.seen_host = atyp, host

            if self.refuse:
                sock.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                return

            onward = socket.create_connection((host, port), timeout=5)
            sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            pump = gevent.spawn(self._relay, sock, onward)
            self._relay(onward, sock)
            pump.join(timeout=3)
        except (ConnectionError, OSError, AssertionError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _relay(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass


class EchoTarget:
    """The site at the far end: sends a fixed payload and closes."""

    def __init__(self):
        self._server = StreamServer(("127.0.0.1", 0), self._handle)
        self._server.start()
        self.port = self._server.server_port

    def stop(self):
        self._server.stop(timeout=3)

    @staticmethod
    def _handle(sock, _address):
        try:
            sock.sendall(PAYLOAD)
        finally:
            sock.close()


def socks5_connect(port, target_host, target_port, atyp=0x03, command=0x01):
    """Speaks SOCKS5 to the shim and returns (reply_code, socket)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"\x05\x01\x00")                      # one method: no auth
    greeting = sock.recv(2)
    assert greeting == b"\x05\x00", greeting

    if atyp == 0x03:
        encoded = target_host.encode()
        address = bytes([0x03, len(encoded)]) + encoded
    else:
        address = bytes([0x01]) + socket.inet_aton(target_host)
    sock.sendall(bytes([0x05, command, 0x00]) + address + struct.pack("!H", target_port))

    reply = sock.recv(4)
    if len(reply) < 4:
        return None, sock
    sock.recv(6)  # BND.ADDR + BND.PORT
    return reply[1], sock


class ShimTestCase(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.target = EchoTarget()
        self.addCleanup(self.upstream.stop)
        self.addCleanup(self.target.stop)

    def shim(self, remote_dns=True):
        return local_socks5_shim(
            "127.0.0.1", self.upstream.port,
            UPSTREAM_USER, UPSTREAM_PASSWORD,
            remote_dns=remote_dns,
        )


class TestHappyPath(ShimTestCase):
    def test_relays_bytes_and_attaches_credentials(self):
        with self.shim() as port:
            code, sock = socks5_connect(port, "localhost", self.target.port)
            self.assertEqual(code, 0x00)
            self.assertEqual(sock.recv(len(PAYLOAD)), PAYLOAD)
            sock.close()
        # The browser authenticated with nothing; the upstream saw our package key.
        self.assertEqual(self.upstream.seen_credentials, (UPSTREAM_USER, UPSTREAM_PASSWORD))

    def test_ipv4_target(self):
        with self.shim() as port:
            code, sock = socks5_connect(port, "127.0.0.1", self.target.port, atyp=0x01)
            self.assertEqual(code, 0x00)
            self.assertEqual(sock.recv(len(PAYLOAD)), PAYLOAD)
            sock.close()

    def test_resolve_mode_decides_who_resolves_the_name(self):
        """
        The point of the resolve mode is *where* the hostname turns into an
        address, so that is what is asserted: with remote_dns the upstream
        receives a domain (ATYP 3) and resolves it at the exit node; without it
        the name is resolved here and an address goes out (ATYP 1 or 4).

        The connection's outcome is deliberately not asserted here - a locally
        resolved "localhost" may well arrive as ::1 while the test target
        listens on IPv4, which says nothing about the shim.
        """
        with self.shim(remote_dns=True) as port:
            _code, sock = socks5_connect(port, "localhost", self.target.port)
            sock.close()
        self.assertEqual(self.upstream.seen_atyp, 0x03)
        self.assertEqual(self.upstream.seen_host, "localhost")

        self.upstream.seen_atyp = None
        with self.shim(remote_dns=False) as port:
            _code, sock = socks5_connect(port, "localhost", self.target.port)
            sock.close()
        self.assertIn(self.upstream.seen_atyp, (0x01, 0x04))
        self.assertNotEqual(self.upstream.seen_host, "localhost")


class TestRefusals(ShimTestCase):
    def test_bind_command_is_refused(self):
        with self.shim() as port:
            code, sock = socks5_connect(port, "localhost", self.target.port, command=0x02)
            self.assertEqual(code, 0x07)  # command not supported
            sock.close()

    def test_upstream_refusal_is_reported_not_hung(self):
        self.upstream.refuse = True
        with self.shim() as port:
            code, sock = socks5_connect(port, "localhost", self.target.port)
            self.assertEqual(code, 0x05)  # connection refused
            sock.close()

    def test_unreachable_upstream_is_reported(self):
        # A port nobody listens on: the shim must answer, not hang.
        with local_socks5_shim("127.0.0.1", 1, UPSTREAM_USER, UPSTREAM_PASSWORD) as port:
            code, sock = socks5_connect(port, "localhost", self.target.port)
            self.assertIn(code, (0x01, 0x04))
            sock.close()


class TestLifecycle(ShimTestCase):
    def test_port_is_closed_after_the_block(self):
        with self.shim() as port:
            pass
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_listener_is_loopback_only(self):
        # Anything reachable from the container network would be an open proxy.
        with self.shim() as port:
            addresses = {
                info[4][0]
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            } - {"127.0.0.1"}
            for address in addresses:
                with self.subTest(address=address):
                    with self.assertRaises(OSError):
                        socket.create_connection((address, port), timeout=2).close()


if __name__ == "__main__":
    unittest.main()
