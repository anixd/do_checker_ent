"""
Unit tests for engine/dns_policy.py - the IPv6 switch.

No network: the filter is installed over a fake resolver, so what comes back is
decided by the test rather than by whatever DNS the machine happens to have. A
live lookup is no substitute - an environment without IPv6 connectivity returns
no AAAA records at all, and the filter would have nothing to drop.

    venv/bin/python -m unittest tests.test_dns_policy -v
"""

import socket
import unittest

from engine import dns_policy
from engine.classify import classify_transport

V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0))


class FakeResolver:
    """Stands in for socket.getaddrinfo and records how it was called."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls.append((host, port, family, flags))
        if not self.answer:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return list(self.answer)


class PolicyTestCase(unittest.TestCase):
    """
    Each test installs the policy itself and the original resolver is restored
    afterwards - the module patches the socket module globally, which would
    otherwise leak into every test that follows in the same process.
    """

    def setUp(self):
        self._saved_getaddrinfo = socket.getaddrinfo
        self._saved_has_ipv6 = socket.has_ipv6
        self.addCleanup(self._restore)

    def _restore(self):
        dns_policy.apply_ipv6_policy(True)
        socket.getaddrinfo = self._saved_getaddrinfo
        socket.has_ipv6 = self._saved_has_ipv6

    def install(self, answer):
        """Puts a fake resolver in place and switches IPv6 off on top of it."""
        resolver = FakeResolver(answer)
        socket.getaddrinfo = resolver
        dns_policy.apply_ipv6_policy(False)
        return resolver


class TestFiltering(PolicyTestCase):
    def test_aaaa_is_dropped_from_a_mixed_answer(self):
        self.install([V6, V4])
        got = socket.getaddrinfo("example.com", 443)
        self.assertEqual(got, [V4])

    def test_ipv4_only_answer_is_untouched(self):
        self.install([V4])
        self.assertEqual(socket.getaddrinfo("example.com", 443), [V4])

    def test_ipv6_only_name_fails_with_a_message_naming_the_switch(self):
        """
        Returning an empty list here would surface several layers up as
        "getaddrinfo returns an empty list", which says nothing about why.
        """
        self.install([V6])
        with self.assertRaises(socket.gaierror) as ctx:
            socket.getaddrinfo("ipv6.example.com", 443)
        self.assertIn("dns.use_ipv6", str(ctx.exception))

    def test_that_failure_is_classified_as_a_dns_error(self):
        # engine/classify.py matches on the exception text, and this is a
        # resolve failure: it must not land on the connect_error catch-all.
        self.install([V6])
        try:
            socket.getaddrinfo("ipv6.example.com", 443)
        except socket.gaierror as e:
            self.assertEqual(classify_transport(e, None), "dns_error")
        else:
            self.fail("expected gaierror")

    def test_explicit_ipv6_family_is_refused_without_asking_the_resolver(self):
        resolver = self.install([V6, V4])
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("example.com", 443, socket.AF_INET6)
        self.assertEqual(resolver.calls, [])

    def test_explicit_ipv4_family_passes_straight_through(self):
        resolver = self.install([V4])
        self.assertEqual(socket.getaddrinfo("example.com", 443, socket.AF_INET), [V4])
        self.assertEqual(resolver.calls[0][2], socket.AF_INET)

    def test_arguments_reach_the_real_resolver_unchanged(self):
        # PySocks passes AI_ADDRCONFIG; losing flags would change behaviour
        # under the filter in a way nothing else would reveal.
        resolver = self.install([V4])
        socket.getaddrinfo("example.com", 443, 0, socket.SOCK_STREAM, 6, socket.AI_ADDRCONFIG)
        self.assertEqual(resolver.calls[0], ("example.com", 443, 0, socket.AI_ADDRCONFIG))

    def test_a_genuine_resolve_failure_is_not_swallowed(self):
        self.install([])
        with self.assertRaises(socket.gaierror) as ctx:
            socket.getaddrinfo("nothing.invalid", 443)
        self.assertIn("Name or service not known", str(ctx.exception))


class TestInstallAndRemove(PolicyTestCase):
    def test_off_then_on_restores_the_original_resolver(self):
        resolver = self.install([V4])
        self.assertIsNot(socket.getaddrinfo, resolver)
        dns_policy.apply_ipv6_policy(True)
        self.assertIs(socket.getaddrinfo, resolver)

    def test_applying_off_twice_does_not_stack_wrappers(self):
        resolver = self.install([V6, V4])
        wrapper = socket.getaddrinfo
        dns_policy.apply_ipv6_policy(False)
        self.assertIs(socket.getaddrinfo, wrapper)
        dns_policy.apply_ipv6_policy(True)
        self.assertIs(socket.getaddrinfo, resolver)

    def test_has_ipv6_is_lowered_and_restored(self):
        socket.has_ipv6 = True
        self.install([V4])
        self.assertFalse(socket.has_ipv6)
        dns_policy.apply_ipv6_policy(True)
        self.assertTrue(socket.has_ipv6)

    def test_ipv6_enabled_reports_the_current_mode(self):
        self.install([V4])
        self.assertFalse(dns_policy.ipv6_enabled())
        dns_policy.apply_ipv6_policy(True)
        self.assertTrue(dns_policy.ipv6_enabled())


if __name__ == "__main__":
    unittest.main()
