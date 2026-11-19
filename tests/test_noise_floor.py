"""session_noise_floor: a count too small to judge is skipped (and said), in the plan, in prepare and in the run."""

import unittest

from tests.support import TARGET, TEST_RULES, device, fleet_data, pair
from tests.test_zero_sessions import InTheRun, prepared_notes
from panupgrade import settings
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet


def small(devices, count):
    for d in devices:
        d["facts"]["session_count"] = count
    return devices


def check_of(plan, serial):
    return next(r for r in next(d for d in plan["devices"] if d["serial"] == serial)["results"] if r["check"] == "session_baseline")


class TheSetting(unittest.TestCase):
    def test_the_default_is_100_and_the_form_offers_it(self):
        from panupgrade import settingsform
        self.assertEqual(settings.load().session_noise_floor, 100)
        self.assertIn("session_noise_floor", settingsform.INFO)
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["session_noise_floor=-1"])


class InThePlanAndPrepare(unittest.TestCase):
    def plan(self, count, **opts):
        s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
        return build_plan(FakeFleet(fleet_data(small(pair("A1", "A2"), count))), TARGET, s, Selection(), rules=TEST_RULES)

    def test_a_small_table_needs_the_acknowledgement_and_the_plan_says_why(self):
        r = check_of(self.plan(12), "A1")
        self.assertEqual(r["status"], "warn")
        self.assertIn("noise floor is 100", r["reason"])

    def test_at_or_above_the_floor_it_is_judged(self):
        self.assertEqual(check_of(self.plan(100), "A1")["status"], "pass")

    def test_lowering_the_floor_judges_a_small_table(self):
        self.assertEqual(check_of(self.plan(12, session_noise_floor=10), "A1")["status"], "pass")

    def test_the_floor_is_part_of_what_is_approved(self):
        self.assertNotEqual(self.plan(500)["hash"], self.plan(500, session_noise_floor=10)["hash"])

    def test_prepare_notes_a_table_below_the_floor(self):
        notes = prepared_notes(fleet_data(small(pair("A1", "A2"), 12)))
        self.assertTrue([n for n in notes if "noise floor of 100" in n["why"]])
        self.assertEqual([n for n in prepared_notes(fleet_data(small(pair("A1", "A2"), 500))) if "sessions" in n["why"]], [])


class InTheRunToo(InTheRun):
    def test_a_small_pair_finishes_and_says_each_check_was_skipped(self):
        journal, result = self.run_it(fleet_data(small(pair("A1", "A2"), 12)))
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertTrue(self.warned(journal, "A1"))

    def test_a_group_of_small_members_finishes_and_says_so(self):
        data = fleet_data(*small([device("S1"), device("S2"), device("S3")], 30))
        journal, result = self.run_it(data, {"S1": "edge", "S2": "edge", "S3": "edge"})
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertTrue(self.warned(journal, "S1"))


if __name__ == "__main__":
    unittest.main()
