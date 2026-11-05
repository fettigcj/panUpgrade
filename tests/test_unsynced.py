"""The sad path: a pair whose passive config is not synchronized can be upgraded, as an acknowledged risk."""

import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, fleet_data, pair
from panupgrade import executor, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet


def plan_for(data, sel=None, **opts):
    opts = {"sync_to_peer": "Disable", "ha_config_unsynced": "caution", **opts}   # without the tool's own sync, this is the accepted-risk path
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, sel or Selection(), TEST_RULES), s


def dev(plan, serial):
    return next(d for d in plan["devices"] if d["serial"] == serial)


class UnsyncedPairs(unittest.TestCase):
    """The risk is one moment: the active suspends and the upgraded passive takes over with ITS configuration."""

    def data(self):
        return fleet_data(pair("A1", "A2", ha_config_sync=False))

    def codes(self, p):
        return [(c["scope"], c["code"]) for c in p["cautions"]]

    def test_the_defaults_sync_the_pair_themselves_so_there_is_no_failover_risk_to_accept(self):
        s = settings.load(None, [])
        self.assertEqual((s.ha_config_unsynced, s.sync_to_peer), ("block", "Both"))
        p = build_plan(FakeFleet(self.data()), TARGET, s, Selection(), TEST_RULES)
        self.assertEqual(dev(p, "A2")["verdict"], "caution")
        self.assertIn(("A2", "force_config_sync"), self.codes(p))
        self.assertNotIn("failover_to_unsynced_passive", [code for _scope, code in self.codes(p)])

    def test_with_the_sync_disabled_the_default_refuses_an_unsynced_pair(self):
        p = build_plan(FakeFleet(self.data()), TARGET, settings.load(None, ["sync_to_peer=Disable"]), Selection(), TEST_RULES)
        self.assertEqual((dev(p, "A1")["verdict"], dev(p, "A2")["verdict"]), ("blocked", "blocked"))

    def test_choosing_caution_prints_a_loosening_warning(self):
        self.assertTrue(any("ha_config_unsynced=warn" in w for w in settings.load(None, ["ha_config_unsynced=warn"]).loosening_warnings()))
        self.assertFalse(any("ha_config_unsynced" in w for w in settings.load(None, []).loosening_warnings()))

    def test_when_accepted_the_flag_sits_on_the_active_member_and_the_passive_is_not_held_up(self):
        p, _ = plan_for(self.data())
        self.assertEqual(dev(p, "A2")["verdict"], "ready")                     # the passive's own upgrade is harmless
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn(("A1", "failover_to_unsynced_passive"), self.codes(p))
        self.assertNotIn("A2", [scope for scope, code in self.codes(p) if code == "failover_to_unsynced_passive"])

    def test_the_message_says_plainly_what_will_happen_and_when(self):
        p, _ = plan_for(self.data())
        text = next(r["reason"] for r in dev(p, "A1")["results"] if r["check"] == "ha_peer")
        for phrase in ("FAILOVER RISK", "ACTIVE member suspends", "runs ITS OWN configuration", "changes at that moment", "Accepting this when you prepare the batch"):
            self.assertIn(phrase, text)

    def test_block_refuses_to_plan_such_a_pair_and_names_the_way_out(self):
        p, _ = plan_for(self.data(), ha_config_unsynced="block")
        self.assertEqual((dev(p, "A1")["verdict"], dev(p, "A2")["verdict"]), ("blocked", "blocked"))
        self.assertIn("ha_config_unsynced=warn", " ".join(r["reason"] for r in dev(p, "A2")["results"]))

    def test_upgrading_only_the_passive_involves_no_failover_and_so_no_flag(self):
        p, _ = plan_for(self.data(), sel=Selection(serials=["A2"]))
        self.assertNotIn("failover_to_unsynced_passive", [c for _, c in self.codes(p)])
        self.assertEqual(dev(p, "A2")["verdict"], "caution")                    # (only the lopsided-pair caution)

    def test_other_ha_trouble_is_still_a_block(self):
        d = pair("A1", "A2", ha_config_sync=False)
        d[1]["facts"]["ha_session_sync"] = False
        p, _ = plan_for(fleet_data(d))
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")

    def test_forcing_the_sync_still_wins_when_that_is_what_the_user_chose(self):
        p, _ = plan_for(self.data(), sync_to_peer="UpgradeOnly")
        self.assertIn(("A2", "force_config_sync"), self.codes(p))
        self.assertNotIn("failover_to_unsynced_passive", [c for _, c in self.codes(p)])

    def test_the_run_completes_and_the_final_checks_do_not_demand_a_sync_that_cannot_happen(self):
        data = self.data()
        p, s = plan_for(data)
        approval = Approval(p["hash"], ["A1", "A2"], [f"{c['scope']}:{c['code']}" for c in p["cautions"]])
        sim = SimBackend(data)
        with tempfile.TemporaryDirectory() as tmp:
            result = executor.run(p, approval, sim, Journal.create(str(Path(tmp, "j")), p["hash"]), s)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {"A2": "done", "A1": "done"})
        self.assertFalse(sim.nodes["A2"].config_synced)                      # we did not pretend it got synced

    def test_without_the_acknowledgement_the_active_member_is_refused(self):
        data = self.data()
        p, s = plan_for(data)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(executor.ExecutionRefused):
            executor.run(p, Approval(p["hash"], ["A1", "A2"], []), SimBackend(data), Journal.create(str(Path(tmp, "j")), p["hash"]), s)


if __name__ == "__main__":
    unittest.main()
