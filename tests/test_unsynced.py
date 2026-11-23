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


class PluginLevelling(unittest.TestCase):
    """PAN-OS refuses an HA config sync while the members' plugins differ ("Version mismatch with Peer for OC"). plugin_mismatch = alert (default): nothing is touched, the
    plan says what rebalance would do. rebalance: the LOWER-versioned member is brought up to the higher version already in use (a member with none gets the other's version).
    fullupgrade: the highest built-in version goes on both. The HA state's own plugin lines decide (the installed lists miss plugins that run); each member's line shows, as
    its "peer", the version the OTHER member runs."""

    def lopsided(self, builtin=None):
        """The first lab pair, as found: nothing differs in the installed lists; the active's line reads Mismatch / peer Not Installed (it runs a plugin nothing lists)."""
        d = fleet_data(pair("A1", "A2", ha_config_sync=False))
        same = {"dlp": "3.0.10", "vm_series": "5.0.0"}
        d["devices"][0]["facts"]["plugins_installed"] = dict(same)
        d["devices"][1]["facts"]["plugins_installed"] = dict(same)
        d["devices"][0]["facts"]["ha_plugins"] = {"OC": {"local": "Mismatch", "peer": "Not Installed"}}
        d["devices"][0]["facts"]["plugins_builtin"] = {"openconfig": "2.1.0"} if builtin is None else builtin
        d["devices"][1]["facts"]["plugins_builtin"] = {"openconfig": "2.1.0"} if builtin is None else builtin
        return d

    def versions(self, active_runs, passive_runs, builtin="2.1.0"):
        """Both members have a line: each shows the version the other runs."""
        d = fleet_data(pair("A1", "A2", ha_config_sync=False))
        d["devices"][0]["facts"]["ha_plugins"] = {"OC": {"local": "Compat Match", "peer": passive_runs}}
        d["devices"][1]["facts"]["ha_plugins"] = {"OC": {"local": "Compat Match", "peer": active_runs}}
        for dd in d["devices"]:
            dd["facts"]["plugins_builtin"] = {"openconfig": builtin}
        return d

    def facts(self, data):
        fleet = FakeFleet(data)
        return {r.serial: fleet.facts(r.serial) for r in fleet.list_devices()}

    def prepared(self, data, *opts, run=True, ghosts=None, fail=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = settings.load(None, list(opts))
        plan = prepare.build(FakeFleet(data), s, Selection(), TARGET, TEST_RULES)
        if not run:
            return plan, None, None
        sim = SimBackend(data, fail=fail)
        for serial, ghost in (ghosts or {}).items():
            sim.nodes[serial].ghost = dict(ghost)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(tmp.name, "j")), plan["hash"]), s)
        return plan, sim, result

    # ---- what would level two members

    def test_what_would_level_two_installed_lists_only_ever_adds_or_raises(self):
        from panupgrade import xmlops
        self.assertEqual(xmlops.plugin_differences({"openconfig": "2.1.0", "vm_series": "3.0.14"}, {"vm_series": "3.0.14"}),
                         [{"name": "openconfig", "version": "2.1.0", "on": "b", "had": None}])
        self.assertEqual(xmlops.plugin_differences({"vm_series": "3.0.14"}, {"vm_series": "5.0.0"}), [{"name": "vm_series", "version": "5.0.0", "on": "a", "had": "3.0.14"}])
        self.assertEqual(xmlops.plugin_differences({"x": "2.1.10"}, {"x": "2.1.9"}), [{"name": "x", "version": "2.1.10", "on": "b", "had": "2.1.9"}])
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

    def test_rebalance_raises_the_lower_member_to_the_higher_version_already_in_use(self):
        from panupgrade import xmlops
        a, p = (lambda f: (f["A1"], f["A2"]))(self.facts(self.versions("2.0.0", "2.1.0")))      # the second lab pair: the ACTIVE is the lower
        self.assertEqual(xmlops.plugin_fixes(a, p), ([{"name": "openconfig", "version": "2.1.0", "on": "a", "had": "2.0.0"}], ["openconfig"]))
        a, p = (lambda f: (f["A1"], f["A2"]))(self.facts(self.versions("2.1.0", "2.0.0")))
        self.assertEqual(xmlops.plugin_fixes(a, p), ([{"name": "openconfig", "version": "2.1.0", "on": "b", "had": "2.0.0"}], ["openconfig"]))
        a, p = (lambda f: (f["A1"], f["A2"]))(self.facts(self.versions("2.1.0", "2.1.0")))
        self.assertEqual(xmlops.plugin_fixes(a, p), ([], []))

    def test_rebalance_never_introduces_a_third_version_but_fullupgrade_goes_to_the_highest_built_in(self):
        from panupgrade import xmlops
        f = self.facts(self.versions("2.0.0", "2.0.2", builtin="2.1.0"))
        self.assertEqual(xmlops.plugin_fixes(f["A1"], f["A2"])[0], [{"name": "openconfig", "version": "2.0.2", "on": "a", "had": "2.0.0"}])
        self.assertEqual(xmlops.plugin_fixes(f["A1"], f["A2"], "fullupgrade")[0],
                         [{"name": "openconfig", "version": "2.1.0", "on": "b", "had": "2.0.2"}, {"name": "openconfig", "version": "2.1.0", "on": "a", "had": "2.0.0"}])

    def test_a_member_that_lacks_the_plugin_gets_the_others_version_or_else_its_highest_built_in_and_with_neither_nothing_is_guessed(self):
        from panupgrade import xmlops
        f = self.facts(self.lopsided())
        self.assertEqual(xmlops.plugin_fixes(f["A1"], f["A2"]), ([{"name": "openconfig", "version": "2.1.0", "on": "b", "had": None}], ["openconfig"]))
        f = self.facts(self.lopsided(builtin={}))
        self.assertEqual(xmlops.plugin_fixes(f["A1"], f["A2"]), ([], ["openconfig"]))

    # ---- the plan

    def test_the_default_is_alert_and_the_plan_says_what_rebalance_would_install_including_on_the_active(self):
        self.assertEqual(settings.load(None, []).plugin_mismatch, "alert")
        p = build_plan(FakeFleet(self.versions("2.0.0", "2.1.0")), TARGET, settings.load(None, []), Selection(), TEST_RULES)
        for serial in ("A1", "A2"):
            self.assertEqual(dev(p, serial)["verdict"], "blocked", serial)
        reason = next(r["reason"] for r in dev(p, "A2")["results"] if r["check"] == "ha_peer")
        self.assertIn("plugin_mismatch = alert", reason)
        self.assertIn("rebalance `prepare` would install openconfig-2.1.0 on a1 (the ACTIVE member)", reason.replace("with plugin_mismatch = rebalance ", "rebalance "))

    def test_alert_with_ha_config_unsynced_warn_flags_the_failover_on_the_active_member(self):
        p = build_plan(FakeFleet(self.versions("2.0.0", "2.1.0")), TARGET, settings.load(None, ["ha_config_unsynced=warn"]), Selection(), TEST_RULES)
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn(("A1", "failover_to_unsynced_passive"), [(c["scope"], c["code"]) for c in p["cautions"]])
        self.assertNotEqual(dev(p, "A2")["verdict"], "blocked")

    def test_rebalance_holds_the_pair_until_prepare_and_names_the_active_member_when_it_is_the_one_that_changes(self):
        p = build_plan(FakeFleet(self.versions("2.0.0", "2.1.0")), TARGET, settings.load(None, ["plugin_mismatch=rebalance"]), Selection(), TEST_RULES)
        reason = next(r["reason"] for r in dev(p, "A2")["results"] if r["check"] == "ha_peer")
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")
        self.assertIn("Run `prepare` first", reason)
        self.assertIn("openconfig-2.1.0 on a1 (the ACTIVE member)", reason)

    def test_members_whose_plugins_agree_are_planned_as_before(self):
        p = build_plan(FakeFleet(self.versions("2.1.0", "2.1.0")), TARGET, settings.load(None, []), Selection(), TEST_RULES)
        self.assertEqual(dev(p, "A2")["verdict"], "caution")
        self.assertIn(("A2", "force_config_sync"), [(c["scope"], c["code"]) for c in p["cautions"]])

    # ---- prepare

    def test_prepare_with_alert_touches_nothing_and_says_what_rebalance_would_do(self):
        plan, _sim, _ = self.prepared(self.versions("2.0.0", "2.1.0"), run=False)
        self.assertEqual([u for u in plan["units"] if u["sync_config"] or u["plugins"]], [])
        why = " ".join(n["why"] for n in plan["notes"])
        self.assertIn("plugin_mismatch is 'alert'", why)
        self.assertIn("rebalance would install openconfig-2.1.0 on a1 (the ACTIVE member)", why)

    def test_the_approval_prompt_says_plainly_when_the_active_member_is_changed(self):
        plan, _sim, _ = self.prepared(self.versions("2.0.0", "2.1.0"), "plugin_mismatch=rebalance", run=False)
        self.assertEqual(plan["units"][0]["plugins"], [{"serial": "A1", "hostname": "a1", "name": "openconfig", "version": "2.1.0", "had": "2.0.0", "active": True}])
        self.assertIn("(the ACTIVE member: this changes a firewall that is carrying traffic)", " ".join(prepare.describe(plan)))

    def test_rebalance_raises_the_active_and_then_syncs(self):
        plan, sim, result = self.prepared(self.versions("2.0.0", "2.1.0"), "plugin_mismatch=rebalance", ghosts={"A1": {"openconfig": "2.0.0"}, "A2": {"openconfig": "2.1.0"}})
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual([x[:1] + x[2:] for x in sim.calls if x[1] == "install plugin"], [("A1", "openconfig", "2.1.0")])
        self.assertTrue(sim.nodes["A2"].config_synced)
        self.assertEqual([x[1] for x in sim.calls if x[1] in ("install plugin", "sync to peer")], ["install plugin", "sync to peer"])

    def test_a_first_try_on_the_member_that_lacks_it_is_checked_against_what_the_two_lines_then_report(self):
        data = self.lopsided()
        plan, sim, result = self.prepared(data, "plugin_mismatch=rebalance", ghosts={"A1": {"openconfig": "2.0.0"}})      # the active really runs 2.0.0, which nothing lists
        self.assertEqual(plan["units"][0]["plugins"][0]["version"], "2.1.0")                                                # the first try: the highest built-in
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual([x[:1] + x[2:] for x in sim.calls if x[1] == "install plugin"], [("A2", "openconfig", "2.1.0"), ("A1", "openconfig", "2.1.0")])     # up, never down
        self.assertTrue(sim.nodes["A2"].config_synced)

    def test_a_first_try_that_is_already_right_is_installed_once(self):
        plan, sim, result = self.prepared(self.lopsided(), "plugin_mismatch=rebalance", ghosts={"A1": {"openconfig": "2.1.0"}})
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual(len([x for x in sim.calls if x[1] == "install plugin"]), 1)

    def test_fullupgrade_puts_the_highest_version_on_both_members(self):
        plan, sim, result = self.prepared(self.versions("2.0.0", "2.0.2"), "plugin_mismatch=fullupgrade", ghosts={"A1": {"openconfig": "2.0.0"}, "A2": {"openconfig": "2.0.2"}})
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual(sorted((x[0], x[3]) for x in sim.calls if x[1] == "install plugin"), [("A1", "2.1.0"), ("A2", "2.1.0")])
        self.assertTrue(sim.nodes["A2"].config_synced)

    def test_a_plugin_family_without_an_ha_line_is_levelled_from_the_installed_lists(self):
        d = fleet_data(pair("A1", "A2", ha_config_sync=False))
        d["devices"][0]["facts"]["plugins_installed"] = {"network_discovery": "2.0.0"}
        d["devices"][1]["facts"]["plugins_installed"] = {}
        plan, sim, result = self.prepared(d, "plugin_mismatch=rebalance")
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertEqual(sim.nodes["A2"].plugins, {"network_discovery": "2.0.0"})
        self.assertTrue(sim.nodes["A2"].config_synced)

    def test_a_failed_install_stops_that_pair_before_any_sync_and_says_why(self):
        plan, sim, result = self.prepared(self.versions("2.0.0", "2.1.0"), "plugin_mismatch=rebalance",
                                          fail={("A1", "install plugin"): "Installation of openconfig-2.1.0 failed: Minimum software version 12.1.0 required"})
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("Minimum software version", result["outcome"]["A1"][1])
        self.assertNotIn("sync to peer", [x[1] for x in sim.calls])


if __name__ == "__main__":
    unittest.main()
