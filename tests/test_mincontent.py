"""PAN-OS refuses some installs unless the dynamic content is new enough. Seen live: 10.2.1 -> 10.2.2 needs content 8552 or newer."""

import tempfile
import unittest
from pathlib import Path

from tests.support import device, facts, fleet_data
from panupgrade import paths, prepare, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet

RULES = {"verified": True, "direct_from": {}, "stepping_install": {}, "min_content": {"10.2.2": 8552}}


def fw(serial, content, version="10.2.1"):
    return device(serial, version=version, facts=facts(sw_version=version, content_versions={"app": content, "threat": content, "av": "0", "wildfire": "0"}))


def plan(content, target="10.2.2", **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(fleet_data(fw("FW1", content))), target, s, Selection(accept_standalone_outage=True), RULES)


def check(p):
    return next(r for r in p["devices"][0]["results"] if r["check"] == "content_minimum")


class Rule(unittest.TestCase):
    def test_the_requirement_is_the_highest_among_the_installed_steps_and_ignores_hotfix_numbers(self):
        steps = [{"version": "10.2.0", "install": False}, {"version": "10.2.2-h4", "install": True}]
        self.assertEqual(paths.content_requirement(RULES, steps), (8552, "10.2.2-h4"))
        self.assertEqual(paths.content_requirement(RULES, [{"version": "10.2.3", "install": True}]), (None, None))


class ReleaseNotesFloor(unittest.TestCase):
    rules = dict(RULES, min_content_feature={"10.2": 8614, "12.1": 9007})

    def test_a_release_with_no_exact_figure_uses_the_release_train_floor(self):
        self.assertEqual(paths.content_requirement(self.rules, [{"version": "10.2.3", "install": True}]), (8614, "10.2.3"))
        self.assertEqual(paths.content_requirement(self.rules, [{"version": "12.1.4-h1", "install": True}]), (9007, "12.1.4-h1"))

    def test_the_exact_figure_beats_the_train_floor(self):
        self.assertEqual(paths.content_requirement(self.rules, [{"version": "10.2.2", "install": True}]), (8552, "10.2.2"))

    def test_the_shipped_rules_carry_the_published_floors(self):
        shipped = paths.load_rules()["min_content_feature"]
        self.assertEqual(shipped["12.1"], 9007)
        self.assertEqual(shipped["10.2"], 8614)


class InThePlan(unittest.TestCase):
    def test_the_real_failure_is_now_caught_before_anything_runs(self):
        p = plan("8545-7303")                                   # a real-world figure from a firewall that had content just below the floor
        self.assertEqual(check(p)["status"], "fail")
        self.assertIn("8552", check(p)["reason"])
        self.assertIn("panupgrade prepare", check(p)["reason"])
        self.assertEqual(p["devices"][0]["verdict"], "blocked")

    def test_content_that_is_new_enough_passes(self):
        self.assertEqual(check(plan("9167-10330"))["status"], "pass")
        self.assertEqual(check(plan("8552-1"))["status"], "pass")

    def test_no_content_at_all_counts_as_too_old(self):
        self.assertEqual(check(plan("0"))["status"], "fail")

    def test_a_release_with_no_rule_on_file_is_not_flagged(self):
        self.assertEqual(check(plan("8545-7303", target="10.2.3"))["status"], "skip")


class PrepareFixesIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_lone_firewall_gets_a_content_task_and_ends_meeting_the_minimum(self):
        data = fleet_data(fw("FW1", "8545-7303"))
        s = settings.load()
        pl = prepare.build(FakeFleet(data), s, Selection(), "10.2.2", RULES)
        self.assertEqual([t["serial"] for t in pl["content_tasks"]], ["FW1"])
        self.assertIn("needs 8552 or newer", " ".join(prepare.describe(pl)))
        sim = SimBackend(data)
        result = prepare.run(pl, Approval(pl["hash"], pl["serials"]), sim, Journal.create(str(Path(self.tmp.name, "j")), pl["hash"]), s)
        self.assertEqual(result["outcome"]["FW1"][0], "done")
        self.assertEqual(sim.nodes["FW1"].content["app"], "9999-9999")

    def test_a_firewall_that_already_meets_it_gets_nothing(self):
        pl = prepare.build(FakeFleet(fleet_data(fw("FW1", "9167-10330"))), settings.load(), Selection(), "10.2.2", RULES)
        self.assertEqual(pl["content_tasks"], [])

    def test_sync_content_never_leaves_it_alone(self):
        pl = prepare.build(FakeFleet(fleet_data(fw("FW1", "8545-7303"))), settings.load(None, ["sync_content=never"]), Selection(), "10.2.2", RULES)
        self.assertEqual(pl["content_tasks"], [])

    def test_a_task_that_cannot_reach_the_minimum_fails_loudly(self):
        data = fleet_data(fw("FW1", "8545-7303"))
        s = settings.load()
        pl = prepare.build(FakeFleet(data), s, Selection(), "10.2.2", RULES)

        class Stuck(SimBackend):
            def update_content(self, serial, family):
                self._enter(serial, "update content", family)
                return {}
        result = prepare.run(pl, Approval(pl["hash"], pl["serials"]), Stuck(data), Journal.create(str(Path(self.tmp.name, "j2")), pl["hash"]), s)
        self.assertEqual(result["outcome"]["FW1"][0], "failed")
        self.assertIn("still below the 8552", result["outcome"]["FW1"][1])


if __name__ == "__main__":
    unittest.main()
