"""
Unit tests for the parts of config/loader.py that must not raise.

No files, no environment: these call _build() and _normalize_transport()
directly. Run from the project root:

    python -m unittest tests.test_config -v
"""

import unittest

from config.loader import ALLOWED_TRANSPORTS, ConfigStore


def build(soax: dict):
    """A RootCfg from a partial soax section, with everything else defaulted."""
    return ConfigStore._build({"soax": soax})


def resolve(soax: dict):
    cfg = build(soax)
    warnings = ConfigStore._normalize_transport(cfg)
    return cfg.soax.transport, warnings


class TestTransportDefault(unittest.TestCase):
    def test_explicit_values_are_accepted(self):
        for value in ALLOWED_TRANSPORTS:
            transport, warnings = resolve({"transport": value})
            self.assertEqual(transport, value)
            self.assertEqual(warnings, [])

    def test_value_is_normalised(self):
        transport, _ = resolve({"transport": "  SOCKS5 "})
        self.assertEqual(transport, "socks5")

    def test_missing_means_no_default(self):
        transport, warnings = resolve({})
        self.assertIsNone(transport)
        self.assertTrue(any("No default transport" in w for w in warnings))

    def test_empty_means_no_default(self):
        transport, warnings = resolve({"transport": "   "})
        self.assertIsNone(transport)
        self.assertTrue(any("No default transport" in w for w in warnings))

    def test_garbage_means_no_default_and_never_raises(self):
        # The decision is explicit: a bad value here must not stop the app from
        # starting, because which transport suits the work is the operator's
        # call. It degrades to "choose every time" and says so.
        for value in ("socks4", "https", "http2", "yes", "0", "socks5h"):
            with self.subTest(value=value):
                transport, warnings = resolve({"transport": value})
                self.assertIsNone(transport)
                self.assertTrue(any("Ignoring" in w for w in warnings), warnings)

    def test_deprecated_proxy_type_still_gives_a_default(self):
        # An installation upgrading with `proxy_type: http` in its app.yaml must
        # not silently lose its default.
        transport, warnings = resolve({"proxy_type": "http"})
        self.assertEqual(transport, "http")
        self.assertTrue(any("deprecated" in w for w in warnings), warnings)

    def test_transport_wins_over_deprecated_proxy_type(self):
        transport, _ = resolve({"transport": "socks5", "proxy_type": "http"})
        self.assertEqual(transport, "socks5")

    def test_garbage_in_proxy_type_is_also_survivable(self):
        transport, warnings = resolve({"proxy_type": "socks4"})
        self.assertIsNone(transport)
        self.assertTrue(any("Ignoring" in w for w in warnings), warnings)


class TestIpv6Switch(unittest.TestCase):
    """
    dns.use_ipv6 must land on a real bool, and an unrecognised value must fall
    to the safe side: a non-empty string is truthy in Python, so `"maybe"` would
    otherwise switch IPv6 back on - the opposite of what it says.
    """

    @staticmethod
    def resolve_dns(dns: dict):
        cfg = ConfigStore._build({"dns": dns})
        warnings = ConfigStore._normalize_use_ipv6(cfg)
        return cfg.dns.use_ipv6, warnings

    def test_missing_section_means_disabled(self):
        use_ipv6, warnings = self.resolve_dns({})
        self.assertFalse(use_ipv6)
        self.assertEqual(warnings, [])

    def test_explicit_values_are_kept(self):
        self.assertEqual(self.resolve_dns({"use_ipv6": True})[0], True)
        self.assertEqual(self.resolve_dns({"use_ipv6": False})[0], False)

    def test_strings_yaml_did_not_turn_into_bools_are_read(self):
        # A quoted "true" in app.yaml is a string, and so is an empty value.
        self.assertTrue(self.resolve_dns({"use_ipv6": "yes"})[0])
        self.assertFalse(self.resolve_dns({"use_ipv6": "off"})[0])
        self.assertFalse(self.resolve_dns({"use_ipv6": ""})[0])

    def test_garbage_disables_ipv6_and_warns_instead_of_raising(self):
        use_ipv6, warnings = self.resolve_dns({"use_ipv6": "maybe"})
        self.assertFalse(use_ipv6)
        self.assertTrue(any("dns.use_ipv6" in w for w in warnings), warnings)


class TestUnknownSectionsAreIgnored(unittest.TestCase):
    def test_unknown_keys_do_not_break_the_build(self):
        # An app.yaml left over from the port-mode version must not crash the
        # startup; unknown keys are dropped.
        cfg = ConfigStore._build({"soax": {"port_login": "gone"}, "nonsense": {"x": 1}})
        self.assertEqual(cfg.soax.host, "proxy.soax.com")


if __name__ == "__main__":
    unittest.main()
