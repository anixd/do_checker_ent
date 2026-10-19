"""
Unit tests for engine/countries.py - the ISO 3166-1 alpha-2 check.

Pure data in, string out: no proxy, no network, nothing to mock. What is worth
asserting is the boundary between the three answers a submitted code can get -
usable, wrong shape, right shape but not a country - and that the correction
offered for a near miss is the right one.

    venv/bin/python -m unittest tests.test_countries -v
"""

import unittest

from engine import countries


class TestIsValid(unittest.TestCase):
    def test_accepts_a_real_code_in_any_case(self):
        for raw in ("az", "AZ", " az ", "Az"):
            self.assertTrue(countries.is_valid(raw), raw)

    def test_rejects_reserved_and_misshapen_codes(self):
        for raw in ("zz", "xx", "q1", "zzz", "a", "", None, "uk"):
            self.assertFalse(countries.is_valid(raw), raw)

    def test_accepts_a_code_that_is_not_in_our_catalog(self):
        # The catalog is a hand-kept list of what the dropdown offers; the ISO
        # table is what the gateway can target. The second is the authority here.
        self.assertTrue(countries.is_valid("vu"))


class TestNames(unittest.TestCase):
    def test_name_of_a_real_code(self):
        self.assertEqual(countries.name_of("ua"), "Ukraine")

    def test_name_of_an_unknown_code_is_none(self):
        self.assertIsNone(countries.name_of("zz"))

    def test_apolitical_name_wins_where_the_two_differ(self):
        # The catalog name is what the dropdown shows, so the shorter and less
        # contested form is the useful one.
        self.assertEqual(countries.name_of("tw"), "Taiwan")
        self.assertEqual(countries.name_of("ps"), "Palestine")

    def test_the_qualifier_behind_a_comma_is_dropped(self):
        self.assertEqual(countries.name_of("ir"), "Iran")
        self.assertEqual(countries.name_of("bo"), "Bolivia")
        self.assertEqual(countries.name_of("md"), "Moldova")

    def test_a_trailing_bracket_is_dropped(self):
        self.assertEqual(countries.name_of("fk"), "Falkland Islands")
        self.assertEqual(countries.name_of("mf"), "Saint Martin")

    def test_a_bracket_in_the_middle_is_left_alone(self):
        self.assertEqual(countries.name_of("cc"), "Cocos (Keeling) Islands")

    def test_names_that_would_collide_keep_their_qualifier(self):
        # The dropped half is exactly what tells these two apart.
        self.assertEqual(countries.name_of("vg"), "Virgin Islands, British")
        self.assertEqual(countries.name_of("vi"), "Virgin Islands, U.S.")

    def test_the_long_famous_ones_are_named_by_hand(self):
        # 'United Kingdom of Great Britain and Northern Ireland' has no comma
        # for the rule to work with, and would make the dropdown wider than the
        # page; 'Congo' and 'Korea' each name two countries.
        self.assertEqual(countries.name_of("gb"), "United Kingdom")
        self.assertEqual(countries.name_of("us"), "United States")
        self.assertEqual(countries.name_of("kr"), "South Korea")
        self.assertEqual(countries.name_of("kp"), "North Korea")
        self.assertEqual(countries.name_of("cd"), "DR Congo")
        self.assertEqual(countries.name_of("cg"), "Congo")

    def test_every_name_is_short_enough_for_a_dropdown(self):
        # Uninhabited territories keep their real names; everything a proxy
        # service actually offers has to fit on a line.
        oversized = {
            code: name
            for code, name in countries._DISPLAY_NAMES.items()
            if len(name) > 24
        }
        self.assertEqual(
            sorted(oversized), ["gs", "hm", "io", "pm", "tf", "um", "vc"], oversized
        )


class TestSuggest(unittest.TestCase):
    def test_uk_suggests_gb(self):
        # The one alias worth carrying: 'uk' is not an ISO code, people type it
        # constantly, and the gateway charges for the attempt.
        self.assertEqual(countries.suggest("uk"), "gb")

    def test_alpha3_suggests_its_alpha2(self):
        self.assertEqual(countries.suggest("usa"), "us")
        self.assertEqual(countries.suggest("deu"), "de")

    def test_country_name_suggests_its_code(self):
        self.assertEqual(countries.suggest("germany"), "de")
        self.assertEqual(countries.suggest("GERMANY"), "de")

    def test_nothing_is_suggested_for_actual_nonsense(self):
        for raw in ("zz", "q1", "", None, "qqqqq"):
            self.assertIsNone(countries.suggest(raw), raw)

    def test_no_guessing_by_similarity(self):
        # 'ab' is one letter from several real codes on different continents, so
        # a near-miss suggestion would be worse than none at all.
        self.assertIsNone(countries.suggest("ab"))


class TestErrorFor(unittest.TestCase):
    def test_valid_code_has_no_error(self):
        self.assertIsNone(countries.error_for("az"))

    def test_empty_is_reported_as_required(self):
        self.assertIn("required", countries.error_for(""))

    def test_wrong_shape_and_unknown_code_read_differently(self):
        # Conflating the two leaves the reader guessing which mistake they made.
        self.assertIn("expected 2 letters", countries.error_for("zzz"))
        self.assertIn("not in ISO 3166-1", countries.error_for("zz"))

    def test_message_quotes_what_was_submitted(self):
        self.assertIn("'ZZ'", countries.error_for("ZZ"))

    def test_message_carries_the_correction_when_there_is_one(self):
        self.assertIn("'gb'", countries.error_for("uk"))
        self.assertIn("'us'", countries.error_for("usa"))

    def test_message_has_no_correction_when_there_is_none(self):
        self.assertNotIn("Did you mean", countries.error_for("zz"))


class TestSplitValid(unittest.TestCase):
    def test_partitions_and_keeps_order(self):
        valid, invalid = countries.split_valid(["tr", "zz", "az", "uk"])
        self.assertEqual(valid, ["tr", "az"])
        self.assertEqual(invalid, ["zz", "uk"])

    def test_drops_repeats_and_blanks(self):
        valid, invalid = countries.split_valid(["az", "AZ", " ", "", "zz", "zz"])
        self.assertEqual(valid, ["az"])
        self.assertEqual(invalid, ["zz"])

    def test_empty_input_gives_two_empty_lists(self):
        self.assertEqual(countries.split_valid([]), ([], []))


if __name__ == "__main__":
    unittest.main()
