"""
Unit tests for providers/soax_session.py

No network, no config, no Docker. Run from the project root:

    python -m unittest tests.test_soax_session -v

Expected strings are taken from the dashboard's own connection-string matrix
(country tr, session ID aabbcc), with the city/region/isp rules removed since
this build only targets by country.
"""

import unittest

from providers.soax_session import (
    SoaxConfigError,
    build_username,
    make_session_id,
    sanitize_credentials,
    mask_secret,
)


class TestBuildUsernameMatrix(unittest.TestCase):
    """Mirrors the Rotating rules x Error handling matrix from the dashboard."""

    BASE = dict(country="tr", network="res_mob", session_id="aabbcc")

    def test_no_session_no_error_rule(self):
        # Dashboard: "Per request, rotate after 1 request" + "Rotate"
        self.assertEqual(
            build_username(country="tr", network="res_mob"),
            "country-tr-network-res_mob",
        )

    def test_session_only(self):
        # Dashboard: "On error" + "Rotate"
        self.assertEqual(
            build_username(**self.BASE),
            "country-tr-network-res_mob-session-aabbcc",
        )

    def test_onerror_retry(self):
        # Dashboard: "On error" + "Retry (3 times)"
        self.assertEqual(
            build_username(**self.BASE, on_error="retry", on_error_retries=3),
            "country-tr-network-res_mob-onerror-retry_3-session-aabbcc",
        )

    def test_onerror_fail(self):
        # Dashboard: "On error" + "Fail"
        self.assertEqual(
            build_username(**self.BASE, on_error="fail"),
            "country-tr-network-res_mob-onerror-fail-session-aabbcc",
        )

    def test_bind_node_with_retry(self):
        # Dashboard: "Bind node" + "Retry (3 times)"
        self.assertEqual(
            build_username(
                **self.BASE, on_error="retry", on_error_retries=3, bind_node=True
            ),
            "country-tr-network-res_mob-onerror-retry_3-bind-node-session-aabbcc",
        )

    def test_bind_node_with_fail(self):
        # Dashboard: "Bind node" + "Fail"
        self.assertEqual(
            build_username(**self.BASE, on_error="fail", bind_node=True),
            "country-tr-network-res_mob-onerror-fail-bind-node-session-aabbcc",
        )

    def test_network_variants(self):
        for net in ("res", "mob", "res_mob", "any"):
            with self.subTest(network=net):
                self.assertEqual(
                    build_username(country="tr", network=net, session_id="aabbcc"),
                    f"country-tr-network-{net}-session-aabbcc",
                )


class TestBuildUsernameValidation(unittest.TestCase):

    def test_invalid_country_is_rejected(self):
        # The gateway answers 200 and routes through a random country, which
        # would silently produce a meaningless "available in AZ" result.
        for bad in ("", None, "zzz", "t", "turkey", "t r", "tr1", "t-r"):
            with self.subTest(country=bad):
                with self.assertRaises(SoaxConfigError):
                    build_username(country=bad, network="res_mob")

    def test_country_is_case_normalised(self):
        self.assertEqual(
            build_username(country="  TR  ", network="res_mob"),
            "country-tr-network-res_mob",
        )

    def test_bad_network(self):
        for bad in ("", "wifi", "residential", "res-mob"):
            with self.subTest(network=bad):
                with self.assertRaises(SoaxConfigError):
                    build_username(country="tr", network=bad)

    def test_bind_node_with_replace_is_rejected(self):
        # Gateway: 400 ERR_INVALID_COMBINATION
        with self.assertRaises(SoaxConfigError):
            build_username(
                country="tr", network="res_mob", session_id="aabbcc",
                on_error="replace", bind_node=True,
            )

    def test_bind_node_without_session_is_rejected(self):
        with self.assertRaises(SoaxConfigError):
            build_username(
                country="tr", network="res_mob", on_error="fail", bind_node=True
            )

    def test_retry_count_bounds(self):
        for bad in (0, 11, -1):
            with self.subTest(retries=bad):
                with self.assertRaises(SoaxConfigError):
                    build_username(
                        country="tr", network="res_mob",
                        on_error="retry", on_error_retries=bad,
                    )

    def test_bad_session_id(self):
        for bad in ("has-hyphen", "has_underscore", "UPPER", "x" * 17, "sp ace"):
            with self.subTest(session_id=bad):
                with self.assertRaises(SoaxConfigError):
                    build_username(country="tr", network="res_mob", session_id=bad)


class TestMakeSessionId(unittest.TestCase):

    def test_shape(self):
        self.assertEqual(make_session_id("a1b2c3d4e5f6", 7), "dca1b2c3d407")

    def test_fits_dashboard_limit(self):
        for i in (0, 9, 99, 999):
            with self.subTest(index=i):
                sid = make_session_id("a1b2c3d4e5f6", i)
                self.assertLessEqual(len(sid), 16)
                self.assertTrue(sid.isalnum() and sid.islower())

    def test_uppercase_run_id_is_normalised(self):
        self.assertEqual(make_session_id("A1B2C3D4", 1), "dca1b2c3d401")

    def test_hyphenated_uuid_is_stripped(self):
        # uuid4().hex has no hyphens, but str(uuid4()) does.
        self.assertEqual(make_session_id("a1b2-c3d4-e5f6", 1), "dca1b2c3d401")

    def test_unusable_run_id(self):
        with self.assertRaises(SoaxConfigError):
            make_session_id("----", 1)


class TestSanitizeCredentials(unittest.TestCase):

    def test_strips_proxy_credentials(self):
        raw = (
            "ProxyError: Cannot connect to proxy "
            "http://country-tr-network-res_mob-session-aabbcc:S3cr3tP4ss@proxy.soax.com:1337"
        )
        cleaned = sanitize_credentials(raw)
        self.assertNotIn("S3cr3tP4ss", cleaned)
        self.assertNotIn("session-aabbcc:", cleaned)
        self.assertIn("http://***@proxy.soax.com:1337", cleaned)

    def test_leaves_ordinary_urls_alone(self):
        raw = "Failed to reach https://example.com/path?a=b"
        self.assertEqual(sanitize_credentials(raw), raw)

    def test_handles_empty_and_none(self):
        self.assertEqual(sanitize_credentials(""), "")
        self.assertIsNone(sanitize_credentials(None))

    def test_mask_secret(self):
        self.assertEqual(mask_secret("abcdefghijkl"), "abcd...ijkl")
        self.assertEqual(mask_secret("short"), "*****")
        self.assertEqual(mask_secret(None), "(not set)")


if __name__ == "__main__":
    unittest.main()
