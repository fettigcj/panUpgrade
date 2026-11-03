import json
import tempfile
import unittest
from pathlib import Path

from tests import support  # noqa: F401  (puts src on the path)
from panupgrade import paths, settings
from panupgrade.versions import Version, VersionError


class ExampleSettingsFile(unittest.TestCase):
    def test_the_shipped_example_loads_and_equals_the_defaults(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(settings.load(str(root / "settings.example.toml")), settings.load())


class FeatureKeyTests(unittest.TestCase):
    def test_feature_key_orders_numerically_where_the_text_would_not(self):
        a, b = Version.parse("10.2.1"), Version.parse("10.10.0")
        self.assertLess(a.feature_key, b.feature_key)
        self.assertGreater(a.feature, b.feature)       # why the text form must never be sorted
        self.assertEqual(Version.parse("11.1.13-h3").base, Version(11, 1, 0))


class VersionTests(unittest.TestCase):
    def test_parse_and_order(self):
        self.assertEqual(Version.parse("20.2.9-h1"), Version(20, 2, 9, 1))
        self.assertLess(Version.parse("20.2.9"), Version.parse("20.2.9-h1"))
        self.assertLess(Version.parse("20.2.9-h1"), Version.parse("20.2.10"))
        self.assertLess(Version.parse("20.2.10"), Version.parse("21.0.0"))
        self.assertEqual(Version.parse("20.2.9-h1").feature, "20.2")

    def test_round_trip(self):
        for text in ("20.2.0", "20.2.9-h3", "21.10.12"):
            self.assertEqual(str(Version.parse(text)), text)

    def test_odd_strings_raise_instead_of_guessing(self):
        for bad in ("20.2", "abc", "", "20.2.9-h", "20.2.9-c5", "20.2.9.1", " ", None, 5):
            with self.assertRaises(VersionError, msg=repr(bad)):
                Version.parse(bad)


class SettingsTests(unittest.TestCase):
    def test_defaults_match_owner_decisions(self):
        s = settings.load()
        self.assertEqual((s.max_failures, s.pending_changes, s.spread_limit), (5, "block", 2))
        self.assertEqual((s.failover_band, s.post_upgrade_band), (0.30, 0.50))
        self.assertEqual((s.restore_roles, s.image_source, s.max_parallel), ("leave", "firewall", 5))

    def test_string_numbers_are_rejected_not_compared_later(self):
        # the old script's --maxUpgrades had no type and crashed later on a string
        for bad in ({"max_failures": "5"}, {"max_failures": True}, {"failover_band": "0.3"}):
            with self.assertRaises(settings.SettingsError):
                settings.from_dict(bad)
        with self.assertRaises(settings.SettingsError):
            settings.load(overrides=["max_failures=abc"])
        self.assertEqual(settings.load(overrides=["max_failures=7"]).max_failures, 7)

    def test_secrets_never_live_in_settings(self):
        for key in ("password", "api_key", "token", "ssh_secret", "plan_password"):
            with self.assertRaises(settings.SettingsError):
                settings.from_dict({key: "x"})
        for key in ("plan_credentials_file", "api_key_file"):                # no exemption for names ending in _file
            with self.assertRaises(settings.SettingsError):
                settings.from_dict({key: "x"})

    def test_unknown_and_out_of_range(self):
        for bad in ({"nope": 1}, {"pending_changes": "maybe"}, {"failover_band": 0}, {"max_parallel": 0},
                    {"failover_band": 1.5}):
            with self.assertRaises(settings.SettingsError):
                settings.from_dict(bad)

    def test_loosening_is_warned_not_forbidden(self):
        s = settings.from_dict({"pending_changes": "ignore", "allow_downgrade": True})
        self.assertEqual(len(s.loosening_warnings()), 2)
        self.assertEqual(settings.from_dict({"max_parallel": 9}).loosening_warnings(), [])          # running pairs side by side is the design, not a loosening
        self.assertEqual(settings.load().loosening_warnings(), [])

    def test_toml_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "s.toml")
            p.write_text("max_failures = 2\npending_changes = 'warn'\n", encoding="utf-8")
            s = settings.load(p, overrides=["max_failures=9"])
            self.assertEqual((s.max_failures, s.pending_changes), (9, "warn"))


class PathTests(unittest.TestCase):
    SHIPPED = None

    @classmethod
    def setUpClass(cls):
        cls.SHIPPED = paths.load_rules()

    def steps(self, result):
        return [(s["version"], s["install"]) for s in result.steps]

    def test_same_feature_needs_no_rules(self):
        r = paths.compute("20.2.3", "20.2.9", {})
        self.assertTrue(r.ok)
        self.assertEqual([s["version"] for s in r.steps], ["20.2.9"])

    def test_the_shipped_rules_carry_the_documented_skip_rule(self):
        self.assertTrue(self.SHIPPED["verified"])
        self.assertEqual(self.SHIPPED["skip"], {"from": "10.1", "max_skipped": 3, "download_skipped_bases": False})

    WITH_BASES = {"skip": {"from": "10.1", "max_skipped": 3, "download_skipped_bases": True}}

    def test_a_skip_downloads_only_the_targets_base_and_installs_only_the_target(self):
        r = paths.compute("10.2.4", "11.2.1", self.SHIPPED)
        self.assertEqual(self.steps(r), [("11.2.0", False), ("11.2.1", True)])
        self.assertTrue(r.verified)
        self.assertIn("no image of theirs is downloaded", r.reason)

    def test_the_catch_up_run_from_10_2_to_12_1_downloads_the_target_base_and_the_target(self):
        offered = {"10.2": "10.2.0", "11.0": "11.0.1", "11.1": "11.1.0", "11.2": "11.2.0", "12.1": "12.1.0"}      # 11.0.0 was recalled
        r = paths.compute("10.2.10", "12.1.10-h2", self.SHIPPED, bases=offered)
        self.assertEqual(self.steps(r), [("12.1.0", False), ("12.1.10-h2", True)])

    def test_the_rules_can_ask_for_every_skipped_release_base_too_to_settle_the_question_in_the_lab(self):
        offered = {"10.2": "10.2.0", "11.0": "11.0.1", "11.1": "11.1.0", "11.2": "11.2.0", "12.1": "12.1.0"}
        rules = {**self.SHIPPED, **self.WITH_BASES}
        r = paths.compute("10.2.10", "12.1.10-h2", rules, bases=offered)
        self.assertEqual(self.steps(r), [("11.0.1", False), ("11.1.0", False), ("11.2.0", False), ("12.1.0", False), ("12.1.10-h2", True)])

    def test_the_base_is_the_lowest_non_hotfix_version_the_firewall_lists(self):
        listed = ["11.0.0-h1", "11.0.1", "11.0.2", "10.2.0", "10.2.0-h2", "10.2.4", "weird"]
        self.assertEqual(paths.lowest_base_images(listed), {"11.0": "11.0.1", "10.2": "10.2.0"})

    def test_a_release_the_firewall_does_not_list_is_a_refusal_not_a_guess(self):
        r = paths.compute("10.2.4", "11.2.1", self.SHIPPED, bases={"10.2": "10.2.0", "11.0": "11.0.0", "11.1": "11.1.0"})
        self.assertFalse(r.ok)                                             # the target's own release is missing
        self.assertIn("does not list any 11.2 release", r.reason)
        r = paths.compute("10.2.4", "11.2.1", self.SHIPPED, bases={"10.2": "10.2.0", "11.0": "11.0.0", "11.2": "11.2.0"})
        self.assertTrue(r.ok)                                              # a skipped release is not needed, so not missed
        r = paths.compute("10.2.4", "11.2.1", {**self.SHIPPED, **self.WITH_BASES}, bases={"10.2": "10.2.0", "11.0": "11.0.0", "11.2": "11.2.0"})
        self.assertFalse(r.ok)                                             # unless every skipped base is asked for
        self.assertIn("does not list any 11.1 release", r.reason)

    def test_a_jump_of_more_than_three_releases_stops_at_a_waypoint_it_installs(self):
        rules = {**self.SHIPPED, "legacy": {"preferred": {"11.2": "11.2.4"}, "minimum_build": {}}}
        r = paths.compute("10.1.5", "12.2.1", rules)
        self.assertEqual(self.steps(r), [("11.2.0", False), ("11.2.4", True), ("12.2.0", False), ("12.2.1", True)])
        r = paths.compute("10.1.5", "12.2.1", {**rules, **self.WITH_BASES})
        self.assertEqual(self.steps(r), [("10.2.0", False), ("11.0.0", False), ("11.1.0", False), ("11.2.0", False), ("11.2.4", True),
                                         ("12.1.0", False), ("12.2.0", False), ("12.2.1", True)])
        r = paths.compute("10.1.5", "12.2.1", self.SHIPPED)
        self.assertFalse(r.ok)
        self.assertIn("not guessing", r.reason)

    def test_below_10_1_every_feature_release_is_stepped_through_and_then_it_skips(self):
        rules = {**self.SHIPPED, "legacy": {"preferred": {"9.1": "9.1.19", "10.0": "10.0.12", "10.1": "10.1.14"}, "minimum_build": {}}}
        r = paths.compute("9.1.8", "11.1.2", rules)
        self.assertEqual(self.steps(r), [("9.1.19", True), ("10.0.0", False), ("10.0.12", True), ("10.1.0", False), ("10.1.14", True),
                                         ("11.1.0", False), ("11.1.2", True)])

    def test_legacy_without_a_pinned_maintenance_release_is_refused(self):
        r = paths.compute("9.1.8", "10.1.14", self.SHIPPED)
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "no_data")

    def test_a_downgrade_needs_the_flag_and_then_follows_the_skip_rule_unverified(self):
        self.assertEqual(paths.compute("11.1.2", "10.1.4", self.SHIPPED).kind, "not_applicable")
        r = paths.compute("11.1.2", "10.1.4", self.SHIPPED, allow_downgrade=True)
        self.assertEqual(self.steps(r), [("10.1.0", False), ("10.1.4", True)])
        self.assertFalse(r.verified)
        far = paths.compute("11.1.2", "9.1.5", self.SHIPPED, allow_downgrade=True)
        self.assertFalse(far.ok)

    def test_not_newer_is_not_applicable(self):
        self.assertEqual(paths.compute("20.2.9", "20.2.9", {}).kind, "not_applicable")
        self.assertEqual(paths.compute("21.0.0", "20.2.9", {}).kind, "not_applicable")

    def test_unverified_flag_is_carried(self):
        rules = {**self.SHIPPED, "verified": False}
        self.assertFalse(paths.compute("10.2.4", "11.2.1", rules).verified)


if __name__ == "__main__":
    unittest.main()
