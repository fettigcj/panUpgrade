"""The preemption window (disable_preemption): planned, recorded, set to no, put back, and reported when it cannot be, against the simulator."""

import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, fleet_data, pair
from panupgrade import executor, settings
from panupgrade.approval import Approval
from panupgrade.executor import BackendError
from panupgrade.journal import Journal
from panupgrade.ledger import Ledger
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet
from panupgrade.windows import restore_leftovers


def plan_for(data, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, Selection(), rules=TEST_RULES), s


def approve(plan):
    return Approval(plan["hash"], [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
                    [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])


class Planning(unittest.TestCase):
    def test_nothing_is_planned_by_default(self):
        plan, s = plan_for(fleet_data(pair("A1", "A2", preemption=True)))
        self.assertFalse(s.disable_preemption)
        self.assertEqual(plan["preemption"], [])
        self.assertNotIn("preemption_window", [c["code"] for c in plan["cautions"]])

    def test_a_pair_with_preemption_on_gets_a_window_and_a_caution_to_acknowledge(self):
        plan, _ = plan_for(fleet_data(pair("A1", "A2", preemption=True)), disable_preemption="true")
        self.assertEqual([w["serials"] for w in plan["preemption"]], [["A2", "A1"]])
        caution = next(c for c in plan["cautions"] if c["code"] == "preemption_window")
        self.assertIn("does not hand the active role back", caution["text"])

    def test_a_pair_with_preemption_off_is_left_alone(self):
        plan, _ = plan_for(fleet_data(pair("A1", "A2", preemption=False)), disable_preemption="true")
        self.assertEqual(plan["preemption"], [])

    def test_the_setting_is_in_the_loosening_warnings_and_the_settings_form(self):
        from panupgrade import settingsform
        s = settings.load(None, ["disable_preemption=true"])
        self.assertTrue(any("disable_preemption" in w for w in s.loosening_warnings()))
        self.assertIn("disable_preemption", [name for _, names in settingsform.SECTIONS for name in names])


class Running(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jpath = str(Path(self._tmp.name, "run.jsonl"))

    def go(self, backend_class=SimBackend, fail=None, **opts):
        data = fleet_data(pair("A1", "A2", preemption=True))
        plan, s = plan_for(data, **{"disable_preemption": "true", **opts})
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        backend = backend_class(data, fail=fail)
        journal = Journal.create(self.jpath, plan["hash"])
        return plan, backend, journal, executor.run(plan, approve(plan), backend, journal, s)

    def ops(self, backend, name):
        return [i for i, call in enumerate(backend.calls) if call[1] == name]

    def test_preemption_is_off_before_the_first_member_is_touched_and_back_after_the_last(self):
        _, sim, journal, result = self.go()
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        sets, suspends, reboots = self.ops(sim, "set preemption"), self.ops(sim, "suspend"), self.ops(sim, "reboot")
        self.assertEqual(len(sets), 4)                                         # two opened, two restored
        self.assertTrue(all(sim.calls[i][2] == "no" for i in sets[:2]))
        self.assertLess(sets[1], min(suspends + reboots))
        self.assertGreater(sets[2], max(reboots))
        for serial in ("A1", "A2"):
            self.assertTrue(sim.nodes[serial].preemption)
            self.assertEqual(sim.nodes[serial].preempt_local, "yes")
        self.assertEqual(result["preemption_unrestored"], [])

    def test_every_change_and_restore_is_journaled_with_the_original_value(self):
        _, _, journal, _ = self.go()
        for serial in ("A1", "A2"):
            steps = [(e["step"], e["state"]) for e in journal.steps_of(serial) if e["step"].startswith("preemption")]
            self.assertEqual(steps, [("preemption open", "started"), ("preemption open", "ok"), ("preemption restore", "started"), ("preemption restore", "ok")])
            self.assertEqual(journal.detail(serial, "preemption open")["original"], "yes")

    def test_a_value_that_was_not_set_is_put_back_by_deleting_it(self):
        class FromTemplate(SimBackend):
            def __init__(self, data, fail=None):
                super().__init__(data, fail=fail)
                for n in self.nodes.values():
                    n.preempt_local, n.template_preemption = None, True
        _, sim, journal, result = self.go(FromTemplate)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(journal.detail("A1", "preemption open")["original"], None)
        for serial in ("A1", "A2"):
            self.assertIsNone(sim.nodes[serial].preempt_local)
            self.assertTrue(sim.nodes[serial].preemption)                      # the template's value applies again

    def test_nothing_is_touched_when_the_setting_is_off(self):
        _, sim, _, result = self.go(disable_preemption="false")
        self.assertEqual(self.ops(sim, "set preemption"), [])
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})

    def test_a_window_that_cannot_open_on_the_second_member_puts_the_first_back_and_holds_the_pair(self):
        _, sim, _, result = self.go(fail={("A1", "set preemption"): "refused"})
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"held"})
        self.assertEqual(self.ops(sim, "suspend"), [])
        for serial in ("A1", "A2"):
            self.assertTrue(sim.nodes[serial].preemption)
        self.assertEqual(result["preemption_unrestored"], [])

    def test_a_member_that_cannot_be_put_back_is_reported_and_remembered(self):
        class WontRestore(SimBackend):
            def set_preemption(self, serial, value):
                if value != "no":
                    raise BackendError(f"{serial}: the firewall refused the commit")
                super().set_preemption(serial, value)
        data = fleet_data(pair("A1", "A2", preemption=True))
        plan, s = plan_for(data, disable_preemption="true")
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        ledger = Ledger(Path(self._tmp.name, "ledger.json"))
        sim = WontRestore(data)
        journal = Journal.create(self.jpath, plan["hash"])
        result = executor.run(plan, approve(plan), sim, journal, s, ledger=ledger)
        self.assertEqual(sorted(e["serial"] for e in result["preemption_unrestored"]), ["A1", "A2"])
        self.assertEqual(result["preemption_unrestored"][0]["original"], "yes")
        self.assertEqual(set(ledger.entries()), {"A1", "A2"})                  # the next run knows it is still changed

        # the next run puts it back (restore_leftovers), clears the ledger
        sim.set_preemption = SimBackend.set_preemption.__get__(sim)
        found = restore_leftovers(ledger.entries(), sim, journal, s)
        self.assertEqual([(w, e) for _, w, e in found], [("preemption window", "")] * 2)
        self.assertEqual(ledger.entries(), {})
        self.assertTrue(sim.nodes["A1"].preemption)


if __name__ == "__main__":
    unittest.main()
