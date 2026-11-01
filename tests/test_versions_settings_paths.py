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


class MinSourceTests(unittest.TestCase):
    RULES = {"verified": True, "direct_from": {"21.0": ["20.2"]}, "min_source": {"21.0": {"20.2": "20.2.7"}}}

    def test_a_too_old_source_first_moves_up_inside_its_own_release(self):
        r = paths.compute("20.2.3", "21.0.2", self.RULES)
        self.assertEqual([(s["version"], s["install"]) for s in r.steps],
                         [("20.2.7", True), ("21.0.0", False), ("21.0.2", True)])

    def test_a_new_enough_source_needs_no_extra_step(self):
        r = paths.compute("20.2.8", "21.0.2", self.RULES)
        self.assertEqual([s["version"] for s in r.steps], ["21.0.0", "21.0.2"])


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
    def test_same_feature_needs_no_rules(self):
        r = paths.compute("20.2.3", "20.2.9", {})
        self.assertTrue(r.ok)
        self.assertEqual([s["version"] for s in r.steps], ["20.2.9"])

    def test_shipped_rules_do_not_guess_across_features(self):
        r = paths.compute("20.1.4", "20.2.9", paths.load_rules())
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "no_data")

    def test_cross_feature_with_rules_downloads_base_then_target(self):
        r = paths.compute("20.1.4", "20.2.9", support.TEST_RULES)
        self.assertEqual([(s["version"], s["install"]) for s in r.steps], [("20.2.0", False), ("20.2.9", True)])

    def test_multi_hop_installs_stepping_stone(self):
        r = paths.compute("20.1.4", "21.1.0", support.TEST_RULES)
        self.assertEqual([(s["version"], s["install"]) for s in r.steps], [("20.2.0", True), ("21.1.0", True)])
        r = paths.compute("20.1.4", "21.1.0", {**support.TEST_RULES, "stepping_install": {"20.2": "20.2.4"}})
        self.assertEqual(r.steps[0]["version"], "20.2.4")

    def test_not_newer_is_not_applicable(self):
        self.assertEqual(paths.compute("20.2.9", "20.2.9", {}).kind, "not_applicable")
        self.assertEqual(paths.compute("21.0.0", "20.2.9", {}).kind, "not_applicable")

    def test_unverified_flag_is_carried(self):
        rules = {**support.TEST_RULES, "verified": False}
        self.assertFalse(paths.compute("20.1.4", "20.2.9", rules).verified)

    def test_shipped_rules_are_marked_unverified_and_empty(self):
        rules = paths.load_rules()
        self.assertFalse(rules["verified"])
        self.assertEqual(rules["direct_from"], {})


if __name__ == "__main__":
    unittest.main()
