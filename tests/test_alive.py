"""
Unit tests for engine/alive.py - the pre-run reachability check.

No network and no provider: a stub stands in for the seam, so what is tested is
the policy (how many targets must answer, which country is probed, what happens
when the probe itself cannot run) rather than SOAX.

    venv/bin/python -m unittest tests.test_alive -v
"""

import logging
import unittest
from unittest import mock

from config.loader import ConfigStore
from engine import alive
from engine.tasks import CheckTask, RunOptions, make_channel
from providers.base import AliveProbe


# The check logs every probe, which is right in production and noise in a test
# asserting exactly those outcomes.
_engine_log = logging.getLogger("engine")
_engine_log.addHandler(logging.NullHandler())
_engine_log.propagate = False


def ok(target="http://example.com"):
    return AliveProbe(target=target, ok=True, status=200, ms=100)


def fail(target="http://nope.invalid", error="dns"):
    return AliveProbe(target=target, ok=False, ms=100, error=error)


class StubProvider:
    """Records what it was asked and returns the probes the test wants."""

    name = "stub"

    def __init__(self, probes, raises=None):
        self.probes = probes
        self.raises = raises
        self.seen = None

    def check_alive(self, task, options, targets, timeout_sec, stop_after_ok=0):
        if self.raises:
            raise self.raises
        self.seen = {
            "task": task,
            "options": options,
            "targets": list(targets),
            "timeout_sec": timeout_sec,
            "stop_after_ok": stop_after_ok,
        }
        return self.probes


class TestVerdict(unittest.TestCase):
    def test_enough_successes(self):
        self.assertTrue(alive.verdict([ok(), ok(), fail()], required_ok=2))

    def test_not_enough(self):
        self.assertFalse(alive.verdict([ok(), fail(), fail()], required_ok=2))

    def test_exactly_at_the_threshold(self):
        self.assertTrue(alive.verdict([ok(), fail()], required_ok=1))

    def test_nothing_answered(self):
        self.assertFalse(alive.verdict([fail(), fail(), fail()], required_ok=1))


class TestCountryForRun(unittest.TestCase):
    def test_first_valid_task_wins(self):
        tasks = [
            CheckTask(index=0, url="a.com", country="kz"),
            CheckTask(index=1, url="b.com", country="az"),
        ]
        self.assertEqual(alive._country_for_run(tasks), "kz")

    def test_unparseable_rows_are_skipped(self):
        tasks = [
            CheckTask(index=0, url="badline", parsing_error="Invalid format"),
            CheckTask(index=1, url="b.com", country="az"),
        ]
        self.assertEqual(alive._country_for_run(tasks), "az")

    def test_no_country_at_all(self):
        tasks = [CheckTask(index=0, url="badline", parsing_error="Invalid format")]
        self.assertEqual(alive._country_for_run(tasks), "")


class EnsureAliveTestCase(unittest.TestCase):
    """ConfigStore is loaded from a dict here - no app.yaml on the test host."""

    def configure(self, **alive_check):
        section = {"enabled": True, "required_ok": 2, "timeout_sec": 7}
        section.update(alive_check)
        previous = ConfigStore._cfg
        self.addCleanup(lambda: setattr(ConfigStore, "_cfg", previous))
        cfg = ConfigStore._build({"alive_check": section})
        ConfigStore._normalize_alive_check(cfg)
        ConfigStore._cfg = cfg
        return cfg

    def run_with(self, provider, tasks=None, **alive_check):
        self.configure(**alive_check)
        tasks = tasks or [CheckTask(index=0, url="a.com", country="az")]
        options = RunOptions(channel=make_channel("soax", "http", "remote"), network="res_mob")
        with mock.patch.object(alive, "get_provider", return_value=provider):
            return alive.ensure_proxy_alive(tasks, options)


class TestEnsureProxyAlive(EnsureAliveTestCase):
    def test_two_of_three_is_alive(self):
        provider = StubProvider([ok(), ok(), fail()])
        result = self.run_with(provider)
        self.assertTrue(result.ok)
        self.assertFalse(result.skipped)
        self.assertEqual(result.country, "az")

    def test_one_of_three_stops_the_run(self):
        provider = StubProvider([ok(), fail(), fail()])
        result = self.run_with(provider)
        self.assertFalse(result.ok)
        self.assertIn("1 of 3", result.reason)
        self.assertIn("need 2", result.reason)

    def test_disabled_skips_the_provider_entirely(self):
        provider = StubProvider([fail(), fail(), fail()])
        result = self.run_with(provider, enabled=False)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertIsNone(provider.seen)

    def test_a_run_with_no_valid_country_is_not_probed(self):
        # Every row failed to parse: nothing will reach the provider anyway, and
        # there is no country to build a session with.
        provider = StubProvider([ok(), ok(), ok()])
        tasks = [CheckTask(index=0, url="badline", parsing_error="Invalid format")]
        result = self.run_with(provider, tasks=tasks)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertIsNone(provider.seen)

    def test_targets_and_timeout_come_from_config(self):
        provider = StubProvider([ok(), ok()])
        self.run_with(provider, targets=["http://one.test", "http://two.test"], timeout_sec=7)
        self.assertEqual(provider.seen["targets"], ["http://one.test", "http://two.test"])
        self.assertEqual(provider.seen["timeout_sec"], 7)
        # The provider may stop probing once this many have answered.
        self.assertEqual(provider.seen["stop_after_ok"], 2)

    def test_an_early_stop_is_reported_against_the_configured_targets(self):
        # The provider answered with two probes out of three configured targets
        # because the verdict was already settled; the run still starts.
        provider = StubProvider([ok(), ok()])
        result = self.run_with(provider)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.probes), 2)

    def test_the_probe_task_carries_the_runs_country_and_no_session(self):
        provider = StubProvider([ok(), ok()])
        tasks = [CheckTask(index=0, url="a.com", country="kz")]
        self.run_with(provider, tasks=tasks)
        self.assertEqual(provider.seen["task"].country, "kz")
        # Sessions are the provider's business: it gives each probe its own, so
        # one target failing cannot drag another probe down with it.
        self.assertIsNone(provider.seen["task"].session_id)

    def test_a_provider_that_cannot_probe_is_reported_not_raised(self):
        provider = StubProvider([], raises=RuntimeError("SOAX_PACKAGE_PASSWORD is not set"))
        result = self.run_with(provider)
        self.assertFalse(result.ok)
        self.assertIn("could not be probed", result.reason)


if __name__ == "__main__":
    unittest.main()
