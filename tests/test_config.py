"""
Unit tests for the parts of config/loader.py that must not raise.

No files, no environment: these call _build() and _normalize_transport()
directly. Run from the project root:

    python -m unittest tests.test_config -v
"""

import os
import shutil
import tempfile
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


class TestAliveCheck(unittest.TestCase):
    """
    The fallbacks lean towards keeping the check running: it exists to stop a
    run against a dead provider, so a typo must not quietly switch that
    protection off (решение 16).
    """

    @staticmethod
    def resolve(section: dict):
        cfg = ConfigStore._build({"alive_check": section})
        warnings = ConfigStore._normalize_alive_check(cfg)
        return cfg.alive_check, warnings

    def test_defaults(self):
        ac, warnings = self.resolve({})
        self.assertTrue(ac.enabled)
        self.assertEqual(len(ac.targets), 3)
        self.assertEqual((ac.required_ok, ac.timeout_sec), (2, 10))
        self.assertEqual(warnings, [])

    def test_targets_accept_a_comma_separated_string(self):
        # That is how the environment variable arrives.
        ac, warnings = self.resolve({"targets": "http://a.test, http://b.test"})
        self.assertEqual(ac.targets, ["http://a.test", "http://b.test"])
        self.assertEqual(warnings, [])

    def test_empty_targets_fall_back_to_the_builtin_list(self):
        ac, warnings = self.resolve({"targets": []})
        self.assertEqual(len(ac.targets), 3)
        self.assertTrue(any("targets is empty" in w for w in warnings), warnings)

    def test_required_ok_cannot_exceed_the_targets(self):
        # Otherwise no run could ever start.
        ac, warnings = self.resolve({"targets": ["http://a.test"], "required_ok": 3})
        self.assertEqual(ac.required_ok, 1)
        self.assertTrue(any("exceeds the number of targets" in w for w in warnings), warnings)

    def test_required_ok_below_one_is_raised(self):
        # Zero would make the check pass even when nothing answers.
        ac, warnings = self.resolve({"required_ok": 0})
        self.assertEqual(ac.required_ok, 1)
        self.assertTrue(any("Raised to 1" in w for w in warnings), warnings)

    def test_garbage_values_warn_instead_of_raising(self):
        ac, warnings = self.resolve({"enabled": "maybe", "required_ok": "two", "timeout_sec": "soon"})
        self.assertTrue(ac.enabled)
        self.assertEqual((ac.required_ok, ac.timeout_sec), (2, 10))
        self.assertEqual(len(warnings), 3, warnings)


class ConfigFileTestCase(unittest.TestCase):
    """
    Drives ConfigStore.init() against a real file in a temp DATA_DIR, because
    the things being tested here (duplicate keys, broken YAML, a missing file)
    only exist at the file level.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "config"), exist_ok=True)
        self.path = os.path.join(self.tmp, "config", "app.yaml")

        saved = (ConfigStore._cfg, ConfigStore._warnings, ConfigStore._blocking,
                 ConfigStore._yaml_text, os.environ.get("DATA_DIR"),
                 os.environ.get("LOG_DIR"))
        self.addCleanup(self._restore, saved)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["DATA_DIR"] = self.tmp
        # Otherwise the default /logs is attempted and the host answers
        # "read-only file system", adding a warning to every assertion here.
        os.environ["LOG_DIR"] = os.path.join(self.tmp, "logs")

    @staticmethod
    def _restore(saved):
        cfg, warnings, blocking, yaml_text, data_dir, log_dir = saved
        ConfigStore._cfg, ConfigStore._warnings = cfg, warnings
        ConfigStore._blocking, ConfigStore._yaml_text = blocking, yaml_text
        for name, value in (("DATA_DIR", data_dir), ("LOG_DIR", log_dir)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def without_transport_notice(warnings):
        """
        The "no default transport" notice is expected whenever a fixture omits
        soax.transport, and says nothing about what is being tested.
        """
        return [w for w in warnings if "No default transport" not in w]

    def load(self, text=None):
        if text is not None:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        ConfigStore.init()
        return ConfigStore.get(), ConfigStore.warnings()


class TestDuplicateKeys(ConfigFileTestCase):
    """
    PyYAML keeps the last duplicate and says nothing. We keep the first and say
    where both were: the accidental line is almost always the one added below.
    """

    def test_duplicate_key_keeps_the_first_value(self):
        cfg, warnings = self.load(
            "soax:\n  session_scope: task\n  session_scope: geo\n"
        )
        self.assertEqual(cfg.soax.session_scope, "task")
        self.assertTrue(any("duplicate key 'session_scope'" in w for w in warnings), warnings)

    def test_the_warning_names_both_lines(self):
        _cfg, warnings = self.load(
            "soax:\n  session_scope: task\n  session_scope: geo\n"
        )
        hit = [w for w in warnings if "duplicate key" in w][0]
        self.assertIn("app.yaml:3", hit)        # the duplicate
        self.assertIn("line 2", hit)            # where it was first set

    def test_duplicate_section_keeps_the_first_block(self):
        # The dangerous one: PyYAML drops the whole first block silently, and
        # every key in it falls back to a default nobody chose.
        cfg, warnings = self.load(
            "soax:\n  host: first.example\n  session_scope: geo\n"
            "soax:\n  host: second.example\n"
        )
        self.assertEqual(cfg.soax.host, "first.example")
        self.assertEqual(cfg.soax.session_scope, "geo")
        self.assertTrue(any("duplicate key 'soax'" in w for w in warnings), warnings)

    def test_a_clean_file_produces_no_warnings(self):
        cfg, warnings = self.load("soax:\n  session_scope: geo\n  transport: http\n")
        self.assertEqual(cfg.soax.session_scope, "geo")
        self.assertEqual(warnings, [])


class TestUnreadableConfig(ConfigFileTestCase):
    """The field case: a container that would not start."""

    def test_broken_indentation_starts_on_defaults(self):
        cfg, warnings = self.load("soax:\n  on_error: retry\n   on_error: fail\n")
        self.assertEqual(cfg.soax.on_error, "retry")        # the default
        self.assertTrue(any("could not be parsed" in w for w in warnings), warnings)
        self.assertTrue(any("line 3" in w for w in warnings), warnings)

    def test_a_tab_starts_on_defaults(self):
        _cfg, warnings = self.load("soax:\n\ton_error: retry\n")
        self.assertTrue(any("could not be parsed" in w for w in warnings), warnings)

    def test_a_missing_file_starts_on_defaults(self):
        cfg, warnings = self.load()  # nothing written
        self.assertEqual(cfg.soax.host, "proxy.soax.com")
        self.assertTrue(any("not found" in w for w in warnings), warnings)

    def test_a_document_that_is_not_a_mapping(self):
        _cfg, warnings = self.load("just a string\n")
        self.assertTrue(any("does not contain a mapping" in w for w in warnings), warnings)

    def test_an_empty_file_is_not_a_problem(self):
        cfg, warnings = self.load("")
        self.assertEqual(cfg.soax.host, "proxy.soax.com")
        self.assertEqual(self.without_transport_notice(warnings), [])


class TestRuleNormalization(ConfigFileTestCase):
    """Unusable values become defaults with a warning, instead of stopping the start."""

    def test_bad_values_are_replaced(self):
        cfg, warnings = self.load(
            "soax:\n  on_error: nonsense\n  session_scope: whatever\n"
            "execution:\n  max_concurrency: 0\n"
        )
        self.assertEqual(cfg.soax.on_error, "retry")
        self.assertEqual(cfg.soax.session_scope, "task")
        self.assertEqual(cfg.execution.max_concurrency, 3)
        self.assertEqual(len(self.without_transport_notice(warnings)), 3, warnings)

    def test_retries_outside_the_gateways_range(self):
        cfg, warnings = self.load("soax:\n  on_error: retry\n  on_error_retries: 99\n")
        self.assertEqual(cfg.soax.on_error_retries, 3)
        self.assertTrue(any("1..10" in w for w in warnings), warnings)

    def test_the_combination_the_gateway_rejects(self):
        cfg, warnings = self.load("soax:\n  bind_node: true\n  on_error: replace\n")
        self.assertFalse(cfg.soax.bind_node)
        self.assertTrue(any("bind_node" in w for w in warnings), warnings)


class TestBlockingErrors(ConfigFileTestCase):
    """The app starts either way; only runs are refused."""

    def test_no_package_password_blocks_runs(self):
        saved = os.environ.pop("SOAX_PACKAGE_PASSWORD", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "SOAX_PACKAGE_PASSWORD", saved)
        self.load("soax:\n  session_scope: task\n")
        blocking = ConfigStore.blocking_errors()
        self.assertEqual(len(blocking), 1)
        self.assertIn("SOAX_PACKAGE_PASSWORD", blocking[0])

    def test_a_password_clears_it(self):
        os.environ["SOAX_PACKAGE_PASSWORD"] = "pk_test"
        self.addCleanup(os.environ.pop, "SOAX_PACKAGE_PASSWORD", None)
        self.load("soax:\n  session_scope: task\n")
        self.assertEqual(ConfigStore.blocking_errors(), [])


class TestSaveYaml(ConfigFileTestCase):
    """
    Strict where startup is forgiving: this is someone typing into a form, and
    the kind answer to nonsense is to keep the file they already have.
    """

    GOOD = "soax:\n  session_scope: geo\n  transport: http\n"

    def test_valid_text_is_written(self):
        self.load("soax:\n  session_scope: task\n")
        ok, err, _warnings = ConfigStore.save_yaml(self.GOOD)
        self.assertTrue(ok, err)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.GOOD)

    def test_the_live_config_is_not_replaced(self):
        # решение 18: the file is the next startup's configuration, nothing more.
        cfg, _ = self.load("soax:\n  session_scope: task\n")
        ConfigStore.save_yaml(self.GOOD)
        self.assertEqual(ConfigStore.get().soax.session_scope, "task")
        self.assertIs(ConfigStore.get(), cfg)

    def test_text_with_no_known_section_is_refused(self):
        # дефект 1: this parses into {':::not yaml::': None} and used to be
        # written straight over a working config.
        self.load(self.GOOD)
        ok, err, _ = ConfigStore.save_yaml(":::not yaml:::")
        self.assertFalse(ok)
        self.assertIn("No known configuration section", err)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.GOOD)

    def test_broken_syntax_is_refused(self):
        self.load(self.GOOD)
        ok, err, _ = ConfigStore.save_yaml("foo:\n\tbar: 1\n")
        self.assertFalse(ok)
        self.assertIn("YAML syntax error", err)

    def test_fixable_problems_are_reported_but_saved(self):
        self.load(self.GOOD)
        ok, err, warnings = ConfigStore.save_yaml("soax:\n  on_error: nonsense\n")
        self.assertTrue(ok, err)
        self.assertTrue(any("on_error" in w for w in warnings), warnings)


class TestUnknownSectionsAreIgnored(unittest.TestCase):
    def test_unknown_keys_do_not_break_the_build(self):
        # An app.yaml left over from the port-mode version must not crash the
        # startup; unknown keys are dropped.
        cfg = ConfigStore._build({"soax": {"port_login": "gone"}, "nonsense": {"x": 1}})
        self.assertEqual(cfg.soax.host, "proxy.soax.com")


if __name__ == "__main__":
    unittest.main()
