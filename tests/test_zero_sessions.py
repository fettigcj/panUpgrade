"""A pre-production environment with no sessions: said in the plan and in prepare, skipped (and said so) in the run, never a division by zero."""

import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, device, fleet_data, pair
from panupgrade import console, executor, prepare, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet


def zero(devices):
    for d in devices:
        d["facts"]["session_count"] = 0
    return devices


def prepared_notes(data, groups=None):
    plan = prepare.build(FakeFleet(data), settings.load(), Selection(groups=groups or {}, accept_standalone_outage=True), TARGET, TEST_RULES)
    return plan["notes"]


class InThePlan(unittest.TestCase):
    def plan(self, data):
        return build_plan(FakeFleet(data), TARGET, settings.load(), Selection(accept_standalone_outage=True), rules=TEST_RULES)

    def test_the_plan_names_the_checks_that_will_be_skipped(self):
        p = self.plan(fleet_data(zero(pair("A1", "A2"))))
        check = next(r for r in next(d for d in p["devices"] if d["serial"] == "A1")["results"] if r["check"] == "session_baseline")
        self.assertEqual(check["status"], "warn")
        self.assertIn("SKIPPED", check["reason"])
        self.assertIn("A1:no_session_baseline", [f"{c['scope']}:{c['code']}" for c in p["cautions"]])

    def test_a_stand_alone_with_no_sessions_is_flagged_too(self):
        p = self.plan(fleet_data(zero([device("S1")])))
        self.assertIn("no_session_baseline", [c["code"] for c in p["cautions"]])


class InPrepare(unittest.TestCase):
    def test_prepare_says_so_for_the_active_member_and_not_for_the_passive_one(self):
        notes = prepared_notes(fleet_data(zero(pair("A1", "A2"))))
        said = [n for n in notes if "0 sessions" in n["why"]]
        self.assertEqual([n["serial"] for n in said], ["A1"])

    def test_prepare_says_nothing_when_there_is_traffic(self):
        self.assertEqual([n for n in prepared_notes(fleet_data(pair("A1", "A2"))) if "sessions" in n["why"]], [])

    def test_a_firewall_with_no_session_count_at_all_is_said_too(self):
        d = pair("A1", "A2")
        d[0]["facts"]["session_count"] = None
        self.assertTrue([n for n in prepared_notes(fleet_data(d)) if "no session count" in n["why"]])


class InTheRun(unittest.TestCase):
    def run_it(self, data, groups=None, backend=None):
        s = settings.load()
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        plan = build_plan(FakeFleet(data), TARGET, s, Selection(groups=groups or {}), rules=TEST_RULES)
        approval = Approval(plan["hash"], [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
                            [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal.create(str(Path(tmp, "j")), plan["hash"])
            result = executor.run(plan, approval, backend or SimBackend(data), journal, s)
            return journal, result

    def warned(self, journal, serial):
        return [e for e in journal.steps_of(serial) if e["detail"].get("warning") and "session" in e["detail"]["warning"]]

    def test_a_pair_with_no_sessions_finishes_and_each_skipped_check_says_so(self):
        journal, result = self.run_it(fleet_data(zero(pair("A1", "A2"))))
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertTrue(self.warned(journal, "A1"))
        baseline = next(e for serial in ("A1", "A2") for e in journal.steps_of(serial) if e["step"] == "baseline")
        self.assertEqual(console.describe_event(baseline, {})[0], "warn")

    def test_a_group_with_no_sessions_finishes_and_says_the_check_was_skipped(self):
        data = fleet_data(*zero([device("S1"), device("S2"), device("S3")]))
        journal, result = self.run_it(data, {"S1": "edge", "S2": "edge", "S3": "edge"})
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertTrue(self.warned(journal, "S1"))

    def test_a_group_member_with_no_sessions_of_its_own_is_judged_by_the_others(self):
        data = fleet_data(*[device("S1"), device("S2"), device("S3")])
        data["devices"][0]["facts"]["session_count"] = 0                  # S1 had nothing; the others have 1000 each

        class TakesTrafficBack(SimBackend):
            def reboot_and_wait(self, serial, timeout_s):
                version = super().reboot_and_wait(serial, timeout_s)
                if serial == "S1":
                    self.nodes[serial].sessions = 900                     # the balancer gives it its share once it is back
                return version
        journal, result = self.run_it(data, {"S1": "edge", "S2": "edge", "S3": "edge"}, TakesTrafficBack(data))
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(self.warned(journal, "S1"), [])                  # it was compared with the others' average, nothing skipped

    def test_a_member_that_had_nothing_and_still_has_nothing_while_the_others_carry_traffic_fails(self):
        data = fleet_data(*[device("S1"), device("S2"), device("S3")])
        data["devices"][0]["facts"]["session_count"] = 0
        _, result = self.run_it(data, {"S1": "edge", "S2": "edge", "S3": "edge"})
        self.assertEqual(result["outcome"]["S1"][0], "failed")


if __name__ == "__main__":
    unittest.main()
