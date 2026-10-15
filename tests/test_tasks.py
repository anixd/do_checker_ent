"""
Unit tests for engine/tasks.py

No network, no config, no Docker. Run from the project root:

    python -m unittest tests.test_tasks -v

These cover the parts of a run that are easy to get subtly wrong and expensive
to notice later: deduplication order, per-row parse errors, and the session-id
rules the gateway enforces.
"""

import re
import unittest

from engine.tasks import (
    Channel,
    make_channel,
    assign_session_ids,
    build_all_country_tasks,
    build_multi_geo_tasks,
    build_single_country_tasks,
    normalize_url,
)

SESSION_RE = re.compile(r"^[a-z0-9]{1,16}$")
RUN_ID = "ab12cd34ef56"


class TestNormalizeUrl(unittest.TestCase):
    def test_lowercases_domain_only(self):
        # The path used to be lowercased too, which turned a working /dDjpEn63
        # into a 404.
        self.assertEqual(normalize_url("Domain.COM/LoCaL_PatH"), "domain.com/LoCaL_PatH")

    def test_keeps_scheme_when_given(self):
        self.assertEqual(normalize_url("HTTPS://Example.COM/A"), "https://example.com/A")

    def test_adds_no_scheme_when_absent(self):
        self.assertEqual(normalize_url("example.com"), "example.com")

    def test_keeps_query_and_fragment(self):
        self.assertEqual(
            normalize_url("Example.com/P?A=b&C=D#Frag"), "example.com/P?A=b&C=D#Frag"
        )

    def test_trims_whitespace_and_handles_empty(self):
        self.assertEqual(normalize_url("  example.com  "), "example.com")
        self.assertEqual(normalize_url("   "), "")


class TestSingleCountry(unittest.TestCase):
    def test_indexes_and_lowercases_country(self):
        tasks = build_single_country_tasks("a.com\nb.com", "AZ")
        self.assertEqual([(t.index, t.url, t.country) for t in tasks],
                         [(0, "a.com", "az"), (1, "b.com", "az")])

    def test_dedups_keeping_submitted_order(self):
        tasks = build_single_country_tasks("b.com\na.com\nB.com", "az")
        self.assertEqual([t.url for t in tasks], ["b.com", "a.com"])

    def test_skips_blank_lines(self):
        tasks = build_single_country_tasks("a.com\n\n   \nb.com", "az")
        self.assertEqual(len(tasks), 2)
        self.assertEqual([t.index for t in tasks], [0, 1])


class TestAllCountries(unittest.TestCase):
    def test_url_is_the_outer_loop(self):
        tasks = build_all_country_tasks("a.com\nb.com", ["az", "kz"])
        self.assertEqual([(t.url, t.country) for t in tasks],
                         [("a.com", "az"), ("a.com", "kz"),
                          ("b.com", "az"), ("b.com", "kz")])

    def test_indexes_are_dense_and_ordered(self):
        tasks = build_all_country_tasks("a.com\nb.com", ["az", "kz"])
        self.assertEqual([t.index for t in tasks], [0, 1, 2, 3])

    def test_dedups_countries_and_urls(self):
        tasks = build_all_country_tasks("a.com\nA.com", ["az", "AZ", ""])
        self.assertEqual([(t.url, t.country) for t in tasks], [("a.com", "az")])


class TestMultiGeo(unittest.TestCase):
    def test_parses_pairs(self):
        tasks = build_multi_geo_tasks("a.com az\nb.com tr")
        self.assertEqual([(t.url, t.country) for t in tasks],
                         [("a.com", "az"), ("b.com", "tr")])

    def test_line_without_country_is_a_row_error(self):
        tasks = build_multi_geo_tasks("badline")
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].is_valid)
        self.assertIn("Invalid format", tasks[0].parsing_error)

    def test_misshapen_country_is_a_row_error(self):
        tasks = build_multi_geo_tasks("a.com zzz\nb.com 12")
        self.assertEqual([t.is_valid for t in tasks], [False, False])
        self.assertIn("Invalid country code", tasks[0].parsing_error)

    def test_two_letter_code_is_left_to_the_gateway(self):
        # 'zz' is shape-valid; the gateway answers ERR_INVALID_VALUE for it.
        # Rejecting it here would also reject countries missing from the catalog.
        tasks = build_multi_geo_tasks("a.com zz")
        self.assertTrue(tasks[0].is_valid)
        self.assertEqual(tasks[0].country, "zz")

    def test_dedups_pairs_but_keeps_distinct_geos(self):
        tasks = build_multi_geo_tasks("a.com az\na.com az\na.com tr")
        self.assertEqual([(t.url, t.country) for t in tasks],
                         [("a.com", "az"), ("a.com", "tr")])

    def test_dedups_identical_errors(self):
        tasks = build_multi_geo_tasks("badline\nbadline")
        self.assertEqual(len(tasks), 1)

    def test_one_bad_line_does_not_sink_the_good_ones(self):
        tasks = build_multi_geo_tasks("a.com az\nbadline\nb.com tr")
        self.assertEqual([t.is_valid for t in tasks], [True, False, True])


class TestSessionIds(unittest.TestCase):
    def test_scope_task_gives_every_check_its_own_session(self):
        tasks = build_single_country_tasks("a.com\nb.com\nc.com", "az")
        assign_session_ids(tasks, RUN_ID, "task")
        ids = [t.session_id for t in tasks]
        self.assertEqual(len(set(ids)), 3)
        for session_id in ids:
            self.assertRegex(session_id, SESSION_RE)

    def test_scope_geo_collapses_one_country_to_one_session(self):
        tasks = build_single_country_tasks("a.com\nb.com\nc.com", "az")
        assign_session_ids(tasks, RUN_ID, "geo")
        self.assertEqual(len({t.session_id for t in tasks}), 1)

    def test_scope_geo_keys_on_country_not_position(self):
        # Reusing one id across countries would be rejected with
        # 409 SESSION_PARAMS_MISMATCH, since rules lock on the first request.
        tasks = build_multi_geo_tasks("a.com az\nb.com tr\nc.com az")
        assign_session_ids(tasks, RUN_ID, "geo")
        by_country = {}
        for task in tasks:
            by_country.setdefault(task.country, set()).add(task.session_id)
        self.assertEqual(len(by_country["az"]), 1)
        self.assertEqual(len(by_country["tr"]), 1)
        self.assertNotEqual(by_country["az"], by_country["tr"])

    def test_rows_that_failed_parsing_get_no_session(self):
        tasks = build_multi_geo_tasks("a.com az\nbadline")
        assign_session_ids(tasks, RUN_ID, "task")
        self.assertIsNotNone(tasks[0].session_id)
        self.assertIsNone(tasks[1].session_id)

    def test_session_ids_fit_the_dashboard_limit(self):
        # The dashboard's own Session ID field takes lowercase alphanumerics up
        # to 16 characters, and ids from our logs get pasted into it by hand.
        tasks = build_all_country_tasks("a.com", [f"c{i}" for i in range(12)])
        assign_session_ids(tasks, RUN_ID, "task")
        for task in tasks:
            self.assertLessEqual(len(task.session_id), 16)
            self.assertRegex(task.session_id, SESSION_RE)


class TestChannel(unittest.TestCase):
    def test_default_channel_is_todays_behaviour(self):
        channel = Channel()
        self.assertEqual((channel.provider, channel.transport, channel.resolve),
                         ("soax", "http", "remote"))

    def test_slug_names_provider_and_transport_only(self):
        self.assertEqual(Channel().slug, "soax-http")
        self.assertEqual(Channel(transport="socks5", resolve="local").slug, "soax-socks5")

    def test_proxy_scheme_follows_transport_and_resolve(self):
        # socks5h hands the hostname to the exit node; socks5 resolves here.
        self.assertEqual(Channel(transport="http").proxy_scheme, "http")
        self.assertEqual(Channel(transport="socks5", resolve="remote").proxy_scheme, "socks5h")
        self.assertEqual(Channel(transport="socks5", resolve="local").proxy_scheme, "socks5")

    def test_screenshots_always_go_over_http(self):
        for transport in ("http", "socks5"):
            self.assertEqual(Channel(transport=transport).screenshot_transport, "http")


class TestMakeChannel(unittest.TestCase):
    def test_builds_and_normalises(self):
        channel = make_channel(" SOAX ", "SOCKS5", " Local ")
        self.assertEqual((channel.provider, channel.transport, channel.resolve),
                         ("soax", "socks5", "local"))

    def test_transport_is_required(self):
        # No fallback here on purpose: the caller passes the submitted value or
        # the configured default, and if neither exists the run is refused.
        for missing in (None, "", "   "):
            with self.subTest(value=missing):
                with self.assertRaises(ValueError) as ctx:
                    make_channel("soax", missing, "remote")
                self.assertIn("required", str(ctx.exception))

    def test_unknown_transport_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            make_channel("soax", "socks4", "remote")
        self.assertIn("Unknown transport", str(ctx.exception))

    def test_unknown_resolve_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            make_channel("soax", "socks5", "nowhere")
        self.assertIn("resolve", str(ctx.exception))

    def test_resolve_defaults_to_remote(self):
        self.assertEqual(make_channel("soax", "socks5", None).resolve, "remote")

    def test_socks5_is_refused_when_pysocks_is_missing(self):
        # Without PySocks every socks5 check returns "connect_error: Missing
        # dependencies for SOCKS support", which looks like the site being down.
        # The run is refused up front instead.
        import engine.tasks as tasks_module
        original = tasks_module.SOCKS_SUPPORT_AVAILABLE
        tasks_module.SOCKS_SUPPORT_AVAILABLE = False
        try:
            with self.assertRaises(ValueError) as ctx:
                make_channel("soax", "socks5", "remote")
            self.assertIn("PySocks", str(ctx.exception))
            # http is unaffected
            self.assertEqual(make_channel("soax", "http", "remote").transport, "http")
        finally:
            tasks_module.SOCKS_SUPPORT_AVAILABLE = original


if __name__ == "__main__":
    unittest.main()
