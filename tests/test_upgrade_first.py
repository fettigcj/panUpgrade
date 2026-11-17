"""upgrade_first: which member of an HA pair is upgraded first, in the plan and in the run."""

import unittest

from tests import test_execution as te
from tests.support import TARGET, TEST_RULES, fleet_data, pair
from panupgrade import settings
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet


def plan(data, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, Selection(accept_standalone_outage=True), rules=TEST_RULES)


def dev(p, serial):
    return next(d for d in p["devices"] if d["serial"] == serial)


def actions(p, serial):
    return [s["action"] for s in dev(p, serial)["steps"]]


class InThePlan(unittest.TestCase):
    def test_passive_first_is_the_default_and_the_plan_is_as_it_was(self):
        p = plan(fleet_data(pair("A1", "A2")))
        self.assertEqual(p["order"], [["A2", "A1"]])
        self.assertEqual(p["settings"]["upgrade_first"], "passive")
        self.assertEqual([c["code"] for c in p["cautions"] if c["code"] == "upgrade_first_active"], [])
        self.assertEqual(actions(p, "A1").count("fail over"), 1)
        self.assertEqual(actions(p, "A2").count("fail over"), 0)

    def test_active_first_puts_the_active_member_first_with_two_fail_overs_for_the_pair(self):
        p = plan(fleet_data(pair("A1", "A2")), upgrade_first="active")
        self.assertEqual(p["order"], [["A1", "A2"]])                                  # A1 is the active member
        first, second = actions(p, "A1"), actions(p, "A2")
        self.assertNotIn("wait for the passive firewall's upgrade to finish", first)
        self.assertNotIn("wait for the first member's upgrade to finish", first)
        self.assertLess(first.index("fail over"), next(i for i, a in enumerate(first) if a.startswith("install ")))
        self.assertEqual(second[second.index("backup") + 1], "wait for the first member's upgrade to finish")
        self.assertLess(second.index("fail over"), next(i for i, a in enumerate(second) if a.startswith("install ")))
        self.assertIn("go live", first)
        self.assertIn("go live", second)
        self.assertEqual(p["summary"]["failover_steps"], 2)

    def test_the_plan_says_so_and_the_hash_changes(self):
        a, b = plan(fleet_data(pair("A1", "A2"))), plan(fleet_data(pair("A1", "A2")), upgrade_first="active")
        self.assertIn("upgrade_first_active", [c["code"] for c in b["cautions"]])
        self.assertNotEqual(a["hash"], b["hash"])
        self.assertEqual((dev(b, "A1")["position"], dev(b, "A2")["position"]), ("first", "second"))

    def test_a_firewall_with_no_passive_member_is_not_affected(self):
        from tests.support import device
        a, b = plan(fleet_data(device("S1", version="20.2.3"))), plan(fleet_data(device("S1", version="20.2.3")), upgrade_first="active")
        self.assertEqual(actions(a, "S1"), actions(b, "S1"))
        self.assertNotIn("position", dev(b, "S1"))

    def test_restore_roles_has_nothing_to_do_when_the_original_active_member_ends_active(self):
        p = plan(fleet_data(pair("A1", "A2")), upgrade_first="active", restore_roles="restore")
        self.assertNotIn("restore roles", actions(p, "A1"))


class InTheRun(te.Base):
    def test_the_active_member_is_suspended_first_then_the_other_and_the_original_roles_end_as_they_began(self):
        data = fleet_data(pair("A1", "A2"))
        _, _, sim, _, result = self.go(data, upgrade_first="active")
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        ops = [(c[0], c[1]) for c in sim.calls]
        self.assertLess(ops.index(("A1", "suspend")), ops.index(("A1", "install")))
        self.assertLess(ops.index(("A1", "install")), ops.index(("A2", "suspend")))      # the second member is suspended only after the first is done
        self.assertLess(ops.index(("A2", "suspend")), ops.index(("A2", "install")))
        self.assertEqual((sim.nodes["A1"].version, sim.nodes["A2"].version), (TARGET, TARGET))
        self.assertEqual((sim.nodes["A1"].state, sim.nodes["A2"].state), ("active", "passive"))

    def test_a_passive_member_that_does_not_carry_the_sessions_stops_the_pair_before_anything_is_installed(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1)                                 # the passive member takes over with a tenth of the sessions
        _, _, _, _, result = self.go(data, backend=sim, upgrade_first="active")
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertEqual(result["outcome"]["A2"][0], "held")
        self.assertNotIn(("A1", "install", TARGET), [c[:3] for c in sim.calls])

    def test_when_the_last_member_is_done_a_pair_that_lost_its_sessions_is_reported(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data)
        real = sim.state

        def state(serial):
            got = real(serial)
            if any(c[:2] == ("A2", "reboot") for c in sim.calls) and got["ha_state"] == "active":
                got = dict(got, session_count=1)                                       # service has not come back
            return got
        sim.state = state
        _, _, _, _, result = self.go(data, backend=sim)                              # passive first: A1 is the last member
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("sessions", result["outcome"]["A1"][1])


if __name__ == "__main__":
    unittest.main()
