"""The one-line verdict at the top of the plan, the preparation record and the upgrade record."""

import unittest

from tests.support import TARGET, TEST_RULES, device, fleet_data, pair
from panupgrade import console, report, runlog, settings
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet


def plan_of(*devices):
    return build_plan(FakeFleet(fleet_data(*devices)), TARGET, settings.load(None, []), Selection(accept_standalone_outage=True), rules=TEST_RULES)


class PlanHeadline(unittest.TestCase):
    def test_a_clean_plan_is_ready_and_makes_no_prediction(self):
        level, words, facts = report.headline(plan_of(pair("A1", "A2")))
        self.assertEqual(level, "pass")
        self.assertTrue(words.startswith("READY: 2 to upgrade"))
        text = " ".join([words] + facts).lower()
        for forbidden in ("minutes", "hours", "will succeed", "expected duration"):
            self.assertNotIn(forbidden, text)

    def test_the_headline_leads_the_text_html_and_terminal_plan(self):
        plan = plan_of(pair("A1", "A2"))
        words = report.headline(plan)[1]
        self.assertEqual(report.to_text(plan).splitlines()[0], words)
        self.assertIn(words, report.to_html(plan))
        self.assertIn(words, console.render_plan(plan, console.Style(color=False, width=0)) if hasattr(console, "Style") else words)

    def test_nothing_to_upgrade_is_said_plainly(self):
        plan = plan_of(device("S1", version=TARGET))
        self.assertEqual(report.headline(plan)[0], "skip")
        self.assertTrue(report.headline(plan)[1].startswith("NOTHING TO UPGRADE"))


class RunHeadline(unittest.TestCase):
    def ok(self, **extra):
        return {"outcome": {"A": ("done", None)}, "failures": 0, **extra}

    def test_the_upgrade_record_words(self):
        self.assertEqual(runlog.headline("upgrade", self.ok()), ("pass", "COMPLETED"))
        self.assertEqual(runlog.headline("upgrade", {"outcome": {"A": ("failed", "x")}, "failures": 1}), ("fail", "COMPLETED WITH PROBLEMS"))
        self.assertEqual(runlog.headline("upgrade", self.ok(stopped=True)), ("fail", "STOPPED"))
        self.assertEqual(runlog.headline("upgrade", None), ("fail", "STOPPED"))

    def test_the_preparation_record_words(self):
        self.assertEqual(runlog.headline("preparation", self.ok()), ("pass", "PREPARED"))
        self.assertEqual(runlog.headline("preparation", {"outcome": {"A": ("needs_attention", "x")}, "failures": 0}), ("warn", "PREPARED WITH WARNINGS"))
        self.assertEqual(runlog.headline("preparation", None), ("fail", "NEEDS ATTENTION"))

    def test_the_record_shows_it_first(self):
        data = runlog.build("upgrade", {"target": TARGET, "hash": "h" * 40}, {"A": "a"}, [], "label", "j", self.ok())
        self.assertEqual(runlog.to_text(data).splitlines()[0].split(":")[0], "COMPLETED")
        self.assertIn("COMPLETED", runlog.to_html(data))


if __name__ == "__main__":
    unittest.main()
