"""The sad path: a pair whose passive config is not synchronized can be upgraded, as an acknowledged risk."""

import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, fleet_data, pair
from panupgrade import executor, prepare, settings
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


class PluginsThatDiffer(unittest.TestCase):
    """PAN-OS refuses an HA config sync while the members' INSTALLED plugins differ ("Version mismatch with Peer for OC"). plugin_mismatch = fix (the
    default): `prepare` installs on the member that lacks it the plugin its peer runs, then syncs; the plan says to run prepare. leave: judged like any pair
    that cannot be synced."""

    def data(self, a1=None, a2=None):
        d = fleet_data(pair("A1", "A2", ha_config_sync=False))
        d["devices"][0]["facts"]["plugins_installed"] = {"openconfig": "2.1.0", "vm_series": "3.0.14"} if a1 is None else a1
        d["devices"][1]["facts"]["plugins_installed"] = {"vm_series": "3.0.14"} if a2 is None else a2
        return d

    def test_what_would_level_two_members_only_ever_adds_or_raises(self):
        from panupgrade import xmlops
        self.assertEqual(xmlops.plugin_differences({"openconfig": "2.1.0", "vm_series": "3.0.14"}, {"vm_series": "3.0.14"}),
                         [{"name": "openconfig", "version": "2.1.0", "on": "b", "had": None}])
        self.assertEqual(xmlops.plugin_differences({"vm_series": "3.0.14"}, {"vm_series": "5.0.0"}), [{"name": "vm_series", "version": "5.0.0", "on": "a", "had": "3.0.14"}])
        self.assertEqual(xmlops.plugin_differences({"x": "2.1.10"}, {"x": "2.1.9"}), [{"name": "x", "version": "2.1.10", "on": "b", "had": "2.1.9"}])
        self.assertEqual(xmlops.plugin_differences({"x": "2.1.3-h1"}, {"x": "2.1.3"}), [{"name": "x", "version": "2.1.3-h1", "on": "b", "had": "2.1.3"}])
        self.assertEqual(xmlops.plugin_differences({"x": "1.0.0"}, {"x": "1.0.0"}), [])

    def test_the_installed_list_is_read_not_the_package_flag(self):
        from xml.etree import ElementTree as ET
        from panupgrade import xmlops

        def op(command):
            self.assertEqual(command, "show plugins installed")
            return ET.fromstring("<response status='success'><result><list><entry><type>mgmt</type><name>openconfig</name><version>openconfig-2.1.0</version>"
                                 "<desc>Openconfig</desc></entry><entry><type>virtualization</type><name>vm_series</name><version>vm_series-3.0.14</version></entry>"
                                 "</list></result></response>")
        self.assertEqual(xmlops.parse_plugins_installed(op), {"openconfig": "2.1.0", "vm_series": "3.0.14"})

    def test_the_plan_with_fix_holds_the_pair_until_prepare_has_levelled_the_plugins(self):
        p = build_plan(FakeFleet(self.data()), TARGET, settings.load(None, []), Selection(), TEST_RULES)
        for serial in ("A1", "A2"):
            self.assertEqual(dev(p, serial)["verdict"], "blocked", serial)
            reason = next(r["reason"] for r in dev(p, serial)["results"] if r["check"] == "ha_peer")
            self.assertIn("prepare", reason)
            self.assertIn("openconfig-2.1.0 on a2", reason)

    def test_with_leave_the_pair_is_judged_like_any_pair_that_cannot_be_synced(self):
        p = build_plan(FakeFleet(self.data()), TARGET, settings.load(None, ["plugin_mismatch=leave"]), Selection(), TEST_RULES)
        self.assertEqual((dev(p, "A1")["verdict"], dev(p, "A2")["verdict"]), ("blocked", "blocked"))
        self.assertIn("plugin_mismatch = leave", next(r["reason"] for r in dev(p, "A1")["results"] if r["check"] == "ha_peer"))
        p = build_plan(FakeFleet(self.data()), TARGET, settings.load(None, ["plugin_mismatch=leave", "ha_config_unsynced=warn"]), Selection(), TEST_RULES)
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn(("A1", "failover_to_unsynced_passive"), [(c["scope"], c["code"]) for c in p["cautions"]])
        self.assertNotEqual(dev(p, "A2")["verdict"], "blocked")

    def test_members_whose_plugins_match_are_planned_as_before(self):
        p = build_plan(FakeFleet(self.data(a2={"openconfig": "2.1.0", "vm_series": "3.0.14"})), TARGET, settings.load(None, []), Selection(), TEST_RULES)
        self.assertEqual(dev(p, "A2")["verdict"], "caution")
        self.assertIn(("A2", "force_config_sync"), [(c["scope"], c["code"]) for c in p["cautions"]])

    def test_prepare_plans_the_install_on_the_member_that_lacks_the_plugin_and_then_the_sync(self):
        plan = prepare.build(FakeFleet(self.data()), settings.load(None, []), Selection(), TARGET, TEST_RULES)
        self.assertEqual(plan["units"][0]["plugins"], [{"serial": "A2", "hostname": "a2", "name": "openconfig", "version": "2.1.0", "had": None}])
        self.assertTrue(plan["units"][0]["sync_config"])
        self.assertIn("install the plugin openconfig-2.1.0", " ".join(prepare.describe(plan)))

    def test_prepare_with_leave_touches_no_plugin_and_does_not_try_the_sync(self):
        plan = prepare.build(FakeFleet(self.data()), settings.load(None, ["plugin_mismatch=leave"]), Selection(), TARGET, TEST_RULES)
        self.assertEqual([u for u in plan["units"] if u["sync_config"] or u["plugins"]], [])
        self.assertTrue(any("openconfig" in n["why"] and "leave" in n["why"] for n in plan["notes"]))

    def test_prepare_installs_the_plugin_and_only_then_syncs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = self.data()
        s = settings.load(None, [])
        plan = prepare.build(FakeFleet(data), s, Selection(), TARGET, TEST_RULES)
        sim = SimBackend(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(tmp.name, "j")), plan["hash"]), s)
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual(sim.nodes["A2"].plugins, {"openconfig": "2.1.0", "vm_series": "3.0.14"})
        self.assertTrue(sim.nodes["A2"].config_synced)
        order = [x[1] for x in sim.calls if x[1] in ("install plugin", "sync to peer")]
        self.assertEqual(order, ["install plugin", "sync to peer"])

    def test_a_failed_install_stops_that_pair_before_any_sync(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = self.data()
        s = settings.load(None, [])
        plan = prepare.build(FakeFleet(data), s, Selection(), TARGET, TEST_RULES)
        sim = SimBackend(data, fail={("A2", "install plugin"): "Installation of openconfig-2.1.0 failed: Minimum software version 12.1.0 required"})
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(tmp.name, "j")), plan["hash"]), s)
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("Minimum software version", result["outcome"]["A1"][1] + result["outcome"]["A2"][1])
        self.assertNotIn("sync to peer", [x[1] for x in sim.calls])


if __name__ == "__main__":
    unittest.main()
