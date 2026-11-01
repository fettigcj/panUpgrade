"""Sync-to-peer, downgrade permission, selection criteria, the prepare task, console output, `select` and dry-run."""

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, device, ha_facts, facts, fleet, fleet_data, pair
from panupgrade import cli, console, executor, prepare, settings
from panupgrade.approval import Approval
from panupgrade.executor import ExecutionRefused
from panupgrade.journal import Journal
from panupgrade.model import DeviceRef
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet, snapshot


def unsynced_pair(a="A1", b="A2", **kw):
    return pair(a, b, ha_config_sync=False, **kw)


def plan_for(data, target=TARGET, sel=None, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), target, s, sel or Selection(), rules=TEST_RULES), s


def approve(plan):
    ok = [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")]
    return Approval(plan["hash"], ok, [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])


def dev(plan, serial):
    return next(d for d in plan["devices"] if d["serial"] == serial)


class SyncToPeerPlanning(unittest.TestCase):
    data = staticmethod(lambda: fleet_data(unsynced_pair()))

    def test_disable_blocks_and_says_which_setting_helps(self):
        p, _ = plan_for(self.data(), sync_to_peer="Disable", ha_config_unsynced="block")
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")
        text = " ".join(r["reason"] for r in dev(p, "A2")["results"])
        self.assertIn("sync_to_peer", text)

    def test_both_is_the_default_and_the_upgrade_run_syncs_a_pair_prepare_has_not_reached(self):
        for given in ({}, {"sync_to_peer": "Both"}):
            p, _ = plan_for(self.data(), **given)
            self.assertEqual(dev(p, "A2")["verdict"], "caution", given)
            self.assertIn(("A2", "force_config_sync"), [(c["scope"], c["code"]) for c in p["cautions"]])
            self.assertIn("sync to peer", [s["action"] for s in dev(p, "A2")["steps"]])

    def test_disable_with_caution_plans_the_pair_and_flags_the_failover_moment(self):
        p, _ = plan_for(self.data(), sync_to_peer="Disable", ha_config_unsynced="caution")
        self.assertIn("failover_to_unsynced_passive", [c["code"] for c in p["cautions"]])
        self.assertNotIn("sync to peer", [s["action"] for s in dev(p, "A2")["steps"]])

    def test_upgrade_only_needs_an_acknowledgement_and_adds_the_step_after_the_backup(self):
        p, _ = plan_for(self.data(), sync_to_peer="UpgradeOnly")
        self.assertEqual(dev(p, "A2")["verdict"], "caution")
        self.assertIn(("A2", "force_config_sync"), [(c["scope"], c["code"]) for c in p["cautions"]])
        actions = [s["action"] for s in dev(p, "A2")["steps"]]
        self.assertLess(actions.index("backup"), actions.index("sync to peer"))
        self.assertLess(actions.index("sync to peer"), actions.index(f"install {TARGET}"))
        self.assertNotIn("sync to peer", [s["action"] for s in dev(p, "A1")["steps"]])    # once per pair, on the passive's turn

    def test_other_ha_trouble_still_blocks(self):
        d = unsynced_pair()
        d[1]["facts"]["ha_session_sync"] = False
        p, _ = plan_for(fleet_data(d), sync_to_peer="UpgradeOnly")
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")


class SyncToPeerExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = str(Path(self.tmp.name, "j.jsonl"))

    def test_upgrade_run_syncs_first_then_upgrades_the_pair(self):
        data = fleet_data(unsynced_pair())
        p, s = plan_for(data, sync_to_peer="UpgradeOnly")
        sim = SimBackend(data)
        result = executor.run(p, approve(p), sim, Journal.create(self.j, p["hash"]), s)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {"A2": "done", "A1": "done"})
        calls = [c[:2] for c in sim.calls]
        self.assertIn(("A1", "sync to peer"), calls)
        self.assertLess(calls.index(("A2", "backup")), calls.index(("A1", "sync to peer")))
        self.assertLess(calls.index(("A1", "sync to peer")), calls.index(("A2", "install")))

    def test_a_failed_sync_fails_the_passive_and_holds_the_active(self):
        data = fleet_data(unsynced_pair())
        p, s = plan_for(data, sync_to_peer="UpgradeOnly")
        sim = SimBackend(data, fail={("A1", "sync to peer"): "sync job failed"})
        result = executor.run(p, approve(p), sim, Journal.create(self.j, p["hash"]), s)
        self.assertEqual(result["outcome"]["A2"][0], "failed")
        self.assertEqual(result["outcome"]["A1"][0], "held")
        self.assertNotIn(("A2", "install", TARGET), [c[:3] for c in sim.calls])


class ZeroSessionLab(unittest.TestCase):
    def test_an_acknowledged_zero_session_baseline_does_not_fail_the_failover_or_the_final_check(self):
        d = pair("A1", "A2")
        for x in d:
            x["facts"]["session_count"] = 0
        data = fleet_data(d)
        p, s = plan_for(data)
        self.assertIn(("A1", "no_session_baseline"), [(c["scope"], c["code"]) for c in p["cautions"]])
        with tempfile.TemporaryDirectory() as tmp:
            sim = SimBackend(data)
            result = executor.run(p, approve(p), sim, Journal.create(str(Path(tmp, "j")), p["hash"]), s)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {"A2": "done", "A1": "done"})


class Downgrades(unittest.TestCase):
    def mixed(self):
        return fleet_data(device("OLD", version="20.2.1"), device("SAME", version="20.2.3"), device("NEW", version="20.2.11"),
                          device("FAR", version="21.0.2"))

    def test_off_by_default_newer_firewalls_are_left_alone(self):
        p, _ = plan_for(self.mixed(), target="20.2.3", sel=Selection(accept_standalone_outage=True))
        self.assertEqual(dev(p, "NEW")["verdict"], "skipped")
        self.assertEqual(dev(p, "OLD")["verdict"], "ready")

    def test_allowed_downgrade_needs_an_acknowledgement_and_is_planned_as_an_install(self):
        p, _ = plan_for(self.mixed(), target="20.2.3", sel=Selection(accept_standalone_outage=True), allow_downgrade="true")
        new = dev(p, "NEW")
        self.assertEqual(new["verdict"], "caution")
        self.assertIn(("NEW", "downgrade"), [(c["scope"], c["code"]) for c in p["cautions"]])
        self.assertIn("install 20.2.3", [s["action"] for s in new["steps"]])
        self.assertEqual(dev(p, "SAME")["verdict"], "skipped")        # already the target
        self.assertEqual(dev(p, "OLD")["verdict"], "ready")           # upgrades still upgrade

    def test_cross_feature_downgrade_is_refused_unless_its_own_override_is_set(self):
        p, _ = plan_for(self.mixed(), target="20.2.3", sel=Selection(accept_standalone_outage=True), allow_downgrade="true")
        far = dev(p, "FAR")
        self.assertEqual(far["verdict"], "skipped")
        self.assertIn("allow_cross_feature_downgrade", " ".join(r["reason"] for r in far["results"]))

    def test_cross_feature_override_plans_it_with_a_loud_acknowledgement(self):
        p, _ = plan_for(self.mixed(), target="20.2.3", sel=Selection(accept_standalone_outage=True), allow_downgrade="true",
                        allow_cross_feature_downgrade="true")
        far = dev(p, "FAR")
        self.assertEqual(far["verdict"], "caution")
        self.assertIn(("FAR", "downgrade_cross_feature"), [(c["scope"], c["code"]) for c in p["cautions"]])
        self.assertEqual([s["action"] for s in far["steps"] if s["action"].startswith(("download", "install"))],
                         ["download 20.2.0", "download 20.2.3", "install 20.2.3"])

    def test_the_override_alone_is_a_configuration_error(self):
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["allow_cross_feature_downgrade=true"])

    def test_the_meet_in_the_middle_run_executes_both_directions(self):
        data = fleet_data(device("OLD", version="20.2.1"), device("NEW", version="20.2.11"))
        p, s = plan_for(data, target="20.2.3", sel=Selection(accept_standalone_outage=True), allow_downgrade="true")
        sim = SimBackend(data)
        with tempfile.TemporaryDirectory() as tmp:
            result = executor.run(p, approve(p), sim, Journal.create(str(Path(tmp, "j")), p["hash"]), s)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {"OLD": "done", "NEW": "done"})
        self.assertEqual({n.serial: n.version for n in sim.nodes.values()}, {"OLD": "20.2.3", "NEW": "20.2.3"})

    def test_setting_must_be_a_real_boolean(self):
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["allow_downgrade=maybe"])


class SelectionCriteria(unittest.TestCase):
    def ref(self, name, version, ha=""):
        return DeviceRef("S-" + name, name, "M", version, True, ha_state=ha)

    def test_criteria_combine_and_unreadable_versions_never_match(self):
        refs = [self.ref("site1-fw1", "10.2.1", "active"), self.ref("site1-fw2", "10.2.1", "passive"),
                self.ref("site2-fw1", "10.2.11-h1", "active"), self.ref("odd", "weird")]
        pick = lambda sel: [r.hostname for r in refs if sel.matches(r)]
        self.assertEqual(pick(Selection(name_contains="SITE1")), ["site1-fw1", "site1-fw2"])
        self.assertEqual(pick(Selection(version_is="10.2.1")), ["site1-fw1", "site1-fw2"])
        self.assertEqual(pick(Selection(version_is="10.2.*", ha_states=["passive"])), ["site1-fw2"])
        self.assertEqual(pick(Selection(version_older_than="10.2.5")), ["site1-fw1", "site1-fw2"])
        self.assertEqual(pick(Selection(version_newer_than="10.2.5")), ["site2-fw1"])
        self.assertEqual(Selection().describe(), "everything Panorama manages")
        self.assertIn("hostname contains site1", Selection(name_contains="site1").describe())


class PrepareTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = str(Path(self.tmp.name, "p.jsonl"))
        self.data = fleet_data(unsynced_pair("A1", "A2"), pair("B1", "B2"))
        self.s = settings.load(None, ["sync_to_peer=Both"])

    def build(self, data=None, s=None, target=None):
        return prepare.build(FakeFleet(data or self.data), s or self.s, Selection(), target, TEST_RULES)

    def test_only_pairs_reading_not_synchronized_get_a_sync(self):
        plan = self.build()
        self.assertEqual([u["active"] for u in plan["units"]], ["A1"])
        self.assertEqual(plan["downloads"], [])                    # no target given: nothing to pre-download

    def test_other_sync_settings_leave_the_config_alone_and_say_why(self):
        for mode in ("Disable", "UpgradeOnly"):
            plan = self.build(s=settings.load(None, [f"sync_to_peer={mode}"]))
            self.assertEqual(plan["units"], [], mode)
            self.assertIn("not synchronized", plan["notes"][0]["why"])
        self.assertIn("upgrade run will sync", self.build(s=settings.load(None, ["sync_to_peer=UpgradeOnly"]))["notes"][0]["why"])

    def test_a_target_adds_downloads_only_where_the_image_is_missing(self):
        data = fleet_data(pair("A1", "A2"), pair("B1", "B2"), device("S1", version="20.2.9"))
        data["devices"][2]["facts"]["images_downloaded"] = [TARGET]        # B1 already has it
        plan = self.build(data, target=TARGET)
        got = sorted((d["serial"], d["version"]) for d in plan["downloads"])
        self.assertEqual(got, [("A1", TARGET), ("A2", TARGET), ("B2", TARGET)])
        self.assertIn("S1", [x["serial"] for x in plan["skipped"]])         # already on the target: nothing to do
        self.assertEqual(sorted(plan["serials"]), ["A1", "A2", "B2"])

    def test_runs_backup_then_sync_then_downloads_and_confirms(self):
        plan = self.build(target=TARGET)
        sim = SimBackend(self.data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(self.j, plan["hash"]), self.s)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {s: "done" for s in plan["serials"]})
        calls = [c[:2] for c in sim.calls if c[1] in ("backup", "sync to peer", "download")]
        self.assertLess(calls.index(("A2", "backup")), calls.index(("A1", "sync to peer")))
        self.assertLess(calls.index(("A1", "sync to peer")), calls.index(("A1", "download")))
        self.assertTrue(sim.nodes["A2"].config_synced)
        self.assertIn(TARGET, sim.nodes["B2"].downloaded)
        self.assertNotIn("install", [c[1] for c in sim.calls])             # prepare never installs or reboots
        self.assertNotIn("reboot", [c[1] for c in sim.calls])

    def test_a_failed_download_does_not_stop_the_others_until_the_failure_limit(self):
        plan = self.build(fleet_data(pair("A1", "A2"), pair("B1", "B2")), target=TARGET)
        sim = SimBackend(self.data, fail={("A1", "download"): "disk full"})
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(self.j, plan["hash"]), self.s)
        states = {k: v[0] for k, v in result["outcome"].items()}
        self.assertEqual(states["A1"], "failed")
        self.assertEqual(states["A2"], "done")
        self.assertEqual(result["failures"], 1)

    def test_the_failure_limit_stops_new_tasks(self):
        s = settings.load(None, ["sync_to_peer=Both", "max_failures=1"])
        plan = prepare.build(FakeFleet(fleet_data(pair("A1", "A2"))), s, Selection(), TARGET, TEST_RULES)
        sim = SimBackend(fleet_data(pair("A1", "A2")), fail={("A1", "download"): "disk full", ("A2", "download"): "disk full"})
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(self.j, plan["hash"]), s)
        self.assertEqual((result["outcome"]["A1"][0], result["outcome"]["A2"][0], result["stopped"]), ("failed", "not_started", True))

    def test_resume_skips_what_the_journal_says_is_done(self):
        plan = self.build(target=TARGET)
        approval = Approval(plan["hash"], plan["serials"])
        prepare.run(plan, approval, SimBackend(self.data), Journal.create(self.j, plan["hash"]), self.s)
        sim2 = SimBackend(self.data)
        result = prepare.run(plan, approval, sim2, Journal.resume(self.j, plan["hash"]), self.s)
        self.assertEqual(sim2.calls, [])
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()))

    def test_wrong_hash_or_unnamed_firewalls_are_refused(self):
        plan = self.build(target=TARGET)
        for approval in (Approval("0" * 64, plan["serials"]), Approval(plan["hash"], ["ZZZ"])):
            with self.assertRaises(ExecutionRefused):
                prepare.run(plan, approval, SimBackend(self.data), Journal.create(str(Path(self.tmp.name, approval.plan_hash[:4] + str(len(approval.serials)))), plan["hash"]), self.s)


class ContentSyncBeforeConfigSync(unittest.TestCase):
    """Dynamic content (CDSS) must match between HA members before the config sync is even attempted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def journal(self, plan):
        self.n += 1
        return Journal.create(str(Path(self.tmp.name, f"j{self.n}.jsonl")), plan["hash"])

    def data(self, active_content, passive_content, synced=False):
        d = pair("A1", "A2", ha_config_sync=synced)
        d[0]["facts"]["content_versions"] = dict(active_content)
        d[1]["facts"]["content_versions"] = dict(passive_content)
        return fleet_data(d)

    OLD = {"app": "8545-7303", "threat": "8545-7303", "av": "0", "wildfire": "0", "url": "0000.00.00.000"}
    NEW = {"app": "8770-8365", "threat": "8770-8365", "av": "0", "wildfire": "0", "url": "0000.00.00.000"}

    def build(self, data, **opts):
        s = settings.load(None, [f"{k}={v}" for k, v in {"sync_to_peer": "Both", "content_settle_s": 0, **opts}.items()])
        return prepare.build(FakeFleet(data), s, Selection(), None, TEST_RULES), s

    def test_matching_content_means_no_content_task_and_zeros_count_as_none(self):
        plan, _ = self.build(self.data(self.OLD, dict(self.OLD, av=None)))
        self.assertEqual(plan["units"][0]["content"], [])
        self.assertTrue(plan["units"][0]["sync_config"])

    def test_differing_content_is_planned_for_the_family_and_noted_in_the_plan(self):
        plan, _ = self.build(self.data(self.NEW, self.OLD))
        unit = plan["units"][0]
        self.assertEqual(unit["content"], ["content"])
        self.assertEqual(sorted(unit["content_diffs"]), ["app", "threat"])
        self.assertIn("latest content update", " ".join(prepare.describe(plan)))

    def test_order_is_check_now_then_install_on_both_then_the_gate_then_backup_then_config_sync(self):
        data = self.data(self.NEW, self.OLD)
        plan, s = self.build(data)
        sim = SimBackend(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, self.journal(plan), s)
        self.assertEqual(result["outcome"]["A1"][0], "done")
        kinds = ("refresh updates", "update content", "content versions", "backup", "sync to peer")
        order = [(c[0], c[1]) for c in sim.calls if c[1] in kinds]
        first = {k: next(i for i, o in enumerate(order) if o[1] == k) for k in kinds}
        last_versions = max(i for i, o in enumerate(order) if o[1] == "content versions")
        self.assertLess(first["refresh updates"], first["update content"])
        self.assertLess(first["update content"], last_versions)
        self.assertLess(last_versions, first["backup"])
        self.assertLess(first["backup"], first["sync to peer"])
        self.assertEqual([c[0] for c in order if c[1] == "refresh updates"], ["A2", "A1"])          # 'check now' on BOTH members
        self.assertTrue(all(c[2] == tuple(prepare.ALL_FAMILIES) for c in sim.calls if c[1] == "refresh updates"))   # ... for EVERY kind of update
        self.assertEqual([c[0] for c in order if c[1] == "update content"], ["A2", "A1"])           # passive first, then active
        self.assertTrue(sim.nodes["A2"].config_synced)

    def test_content_that_will_not_level_gates_the_config_sync(self):
        data = self.data(self.NEW, self.OLD)
        plan, s = self.build(data)

        class Stubborn(SimBackend):                       # the install "works" but nothing actually changes
            def update_content(self, serial, family):
                self._enter(serial, "update content", family)
                return {}
        sim = Stubborn(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, self.journal(plan), s)
        state, detail = result["outcome"]["A1"]
        self.assertEqual(state, "failed")
        self.assertIn("NOT attempted", detail)
        self.assertNotIn("sync to peer", [c[1] for c in sim.calls])            # never even tried
        self.assertFalse(sim.nodes["A2"].config_synced)

    def test_a_failed_content_install_gates_it_too(self):
        data = self.data(self.NEW, self.OLD)
        plan, s = self.build(data)
        sim = SimBackend(data, fail={("A2", "update content"): "no licence"})
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, self.journal(plan), s)
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertNotIn("sync to peer", [c[1] for c in sim.calls])

    def test_with_sync_content_never_a_difference_is_noted_and_still_gates_the_config_sync(self):
        data = self.data(self.NEW, self.OLD)
        plan, s = self.build(data, sync_content="never")
        self.assertEqual(plan["units"][0]["content"], [])
        self.assertIn("sync_content is 'never'", plan["notes"][0]["why"])
        sim = SimBackend(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, self.journal(plan), s)
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertNotIn("sync to peer", [c[1] for c in sim.calls])

    def test_wildfire_one_release_apart_is_normal_and_is_not_a_mismatch(self):
        # seen live on 2026-10-29: 1130925-1135583 on one member and 1130924-1135582 on the other, minutes apart
        a, b = dict(self.OLD, wildfire="1130925-1135583"), dict(self.OLD, wildfire="1130924-1135582")
        plan, _ = self.build(self.data(a, b, synced=True))
        self.assertEqual(plan["units"], [])
        plan, s = self.build(self.data(a, b))                       # unsynced config: the sync is not gated by WildFire
        self.assertTrue(plan["units"][0]["sync_config"])
        self.assertEqual(plan["units"][0]["content"], [])
        sim = SimBackend(self.data(a, b))
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, self.journal(plan), s)
        self.assertEqual(result["outcome"]["A1"][0], "done")

    def test_a_pair_with_matching_content_and_synced_config_needs_nothing(self):
        plan, _ = self.build(self.data(self.OLD, self.OLD, synced=True))
        self.assertEqual(plan["units"], [])


class HaCompatibilityVerdict(unittest.TestCase):
    """PAN-OS's own sanity check ("Unknown" on the lab's 10.2.1 pairs) matters even when the installed versions are equal."""

    UNKNOWN = {"build": "Match", "app": "Unknown", "threat": "Unknown", "av": "Unknown", "url": "Unknown", "iot": "Unknown"}
    SAME = {"app": "8545-7303", "threat": "8545-7303", "av": "0", "wildfire": "0", "url": "0000.00.00.000"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def data(self, compat):
        d = pair("A1", "A2", ha_config_sync=False)
        for x in d:
            x["facts"]["content_versions"] = dict(self.SAME)
            x["facts"]["ha_content_compat"] = dict(compat)
        return fleet_data(d)

    def build(self, data, **opts):
        s = settings.load(None, [f"{k}={v}" for k, v in {"sync_to_peer": "Both", "content_settle_s": 0, **opts}.items()])
        return prepare.build(FakeFleet(data), s, Selection(), None, TEST_RULES), s

    def test_equal_versions_but_unknown_compatibility_still_plan_a_content_update_first(self):
        plan, _ = self.build(self.data(self.UNKNOWN))
        unit = plan["units"][0]
        self.assertEqual(unit["content"], ["content"])
        self.assertIn("compatibility as app: Unknown", unit["content_reason"])
        self.assertTrue(unit["sync_config"])

    def test_the_update_makes_pan_os_agree_and_then_the_config_sync_runs(self):
        data = self.data(self.UNKNOWN)
        plan, s = self.build(data)
        sim = SimBackend(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(self.tmp.name, "j")), plan["hash"]), s)
        self.assertEqual(result["outcome"]["A1"][0], "done")
        self.assertTrue(sim.nodes["A2"].config_synced)

    def test_check_now_alone_can_settle_it_and_then_nothing_is_installed(self):
        data = self.data(self.UNKNOWN)

        class CheckNowTeachesPanOs(SimBackend):            # the owner's hypothesis: a pair that never asked the update server learns on 'check now'
            def refresh_updates(self, serial, families):
                for node in (self.nodes[serial], self.nodes.get(self.nodes[serial].peer)):
                    node.compat.update({k: "Match" for k in ("app", "threat", "av", "iot")})
                return super().refresh_updates(serial, families)
        plan, s = self.build(data)
        sim = CheckNowTeachesPanOs(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(self.tmp.name, "j3")), plan["hash"]), s)
        self.assertEqual(result["outcome"]["A1"][0], "done")
        self.assertNotIn("update content", [c[1] for c in sim.calls])             # nothing was installed
        self.assertIn("sync to peer", [c[1] for c in sim.calls])

    def test_it_waits_up_to_the_settle_time_for_the_verdict_before_installing(self):
        data = self.data(self.UNKNOWN)
        plan, s = self.build(data, content_settle_s=1, poll_interval_s=1)
        sim = SimBackend(data)
        started = time.monotonic()
        prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(self.tmp.name, "j4")), plan["hash"]), s)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertIn("update content", [c[1] for c in sim.calls])                # it settled for nothing, so it installed

    def test_if_pan_os_still_cannot_confirm_the_content_the_config_sync_is_not_attempted(self):
        data = self.data(self.UNKNOWN)
        plan, s = self.build(data)

        class NothingChanges(SimBackend):
            def update_content(self, serial, family):
                self._enter(serial, "update content", family)
                return {}
        sim = NothingChanges(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(self.tmp.name, "j2")), plan["hash"]), s)
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("PAN-OS reports HA content compatibility", result["outcome"]["A1"][1])
        self.assertNotIn("sync to peer", [c[1] for c in sim.calls])

    def test_a_pair_whose_config_is_already_synchronized_is_left_alone_whatever_the_verdict(self):
        d = self.data(self.UNKNOWN)
        for x in d["devices"]:
            x["facts"]["ha_config_sync"] = True
        plan, _ = self.build(d)
        self.assertEqual(plan["units"], [])

    def test_the_plan_says_what_is_installed_and_what_is_not_instead_of_calling_nothing_identical(self):
        plan, _ = self.build(self.data(self.UNKNOWN))
        text = plan["content_report"][0]["text"]
        self.assertIn("app 8545-7303", text)
        self.assertIn("NONE loaded on either member: antivirus, wildfire", text)
        self.assertIn("may be the image's built-in content", text)
        both_none = {"app": "0", "threat": "0", "av": "0", "wildfire": "0", "url": None}
        d = self.data(self.UNKNOWN)
        for x in d["devices"]:
            x["facts"]["content_versions"] = dict(both_none)
        self.assertIn("nothing; NONE loaded on either member: app, threat, antivirus, wildfire",
                      self.build(d)[0]["content_report"][0]["text"])

    def test_the_url_database_mismatch_does_not_count(self):
        plan, _ = self.build(self.data({"build": "Match", "app": "Match", "threat": "Match", "av": "Match", "url": "Mismatch", "iot": "Match"}))
        self.assertEqual(plan["units"][0]["content"], [])


class OneApprovalCoversContent(unittest.TestCase):
    """One typed word covers everything, content installs included; the prompt just says plainly what they are."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        d = pair("A1", "A2", ha_config_sync=False)
        for x in d:
            x["facts"]["content_versions"] = {"app": "8545-7303", "threat": "8545-7303", "av": "0", "wildfire": "0", "url": None}
            x["facts"]["ha_content_compat"] = {"build": "Match", "app": "Unknown", "threat": "Unknown", "av": "Unknown", "url": "Unknown", "iot": "Unknown"}
        self.fleet = self.dir / "fleet.json"
        self.fleet.write_text(json.dumps(fleet_data(d)), encoding="utf-8")

    def run_prepare(self, answers, *extra):
        from unittest import mock

        class Tty(io.StringIO):
            def isatty(self):
                return True
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", Tty()), mock.patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["prepare", "--source", f"fake:{self.fleet}", "--serial", "A1", "--serial", "A2", "--set", "sync_to_peer=Both",
                             "--set", "content_settle_s=0", "--journal", str(self.dir / "p.jsonl"), "--kit-dir", str(self.dir / "kit"),
                             "--color", "never", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_a_stop_asked_for_before_the_tasks_start_means_none_of_them_start(self):
        args = cli.parse_args(["prepare", "--source", f"fake:{self.fleet}", "--serial", "A1", "--serial", "A2", "--set", "sync_to_peer=Both",
                               "--set", "content_settle_s=0", "--journal", str(self.dir / "stop.jsonl"), "--color", "never"])
        style = cli._style(args)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bundle = cli._prepare_review(args, style)
            bundle.stop.set()
            code = cli._prepare_execute(args, style, bundle, lambda jp: cli._new_approval(bundle.plan["hash"], bundle.plan["serials"], [], jp, "test"))
        self.assertIn("stopped by you before it started", out.getvalue())
        self.assertNotIn("sync", [e["step"] for e in Journal.resume(str(self.dir / "stop.jsonl"), bundle.plan["hash"]).events if e.get("state") == "ok"])

    def test_one_word_approves_the_content_install_and_the_sync_and_the_prompt_says_what_content_means(self):
        code, out, _ = self.run_prepare(["APPROVE"])            # a second answer would raise StopIteration: it is never asked for
        self.assertEqual(code, 0, out)
        self.assertIn("dynamic CONTENT updates", out)
        self.assertIn("outside your own change schedule", out)
        self.assertIn("a1 + a2", out)
        self.assertIn("compatibility as app: Unknown", out)

    def test_anything_but_the_one_word_does_nothing(self):
        code, out, _ = self.run_prepare(["CONTENT"])
        self.assertEqual(code, 3)
        self.assertNotIn("update content", (self.dir / "p.jsonl").read_text(encoding="utf-8") if (self.dir / "p.jsonl").exists() else "")

    def test_sync_content_never_leaves_content_alone_and_gates_the_config_sync(self):
        code, out, _ = self.run_prepare(["APPROVE"], "--set", "sync_content=never")
        self.assertNotIn("dynamic CONTENT updates", out)
        self.assertIn("NOT attempted", out)
        self.assertEqual(code, 1)

    def test_a_failed_preparation_does_not_veto_the_kit_when_the_unsynced_pair_was_accepted(self):
        code, out, _ = self.run_prepare(["APPROVE"], "--target", TARGET, "--set", "sync_content=never", "--set", "ha_config_unsynced=caution")
        self.assertIn("These preparation steps failed:", out)
        self.assertIn("may not include firewalls that failed to prepare", out)
        self.assertIn("the upgrade kit is ready", out)
        self.assertEqual(code, 0, out)
        self.assertTrue(list((self.dir / "kit").glob("run-upgrade-*")))

    def test_a_failed_preparation_is_reported_and_the_upgrade_runs_own_sync_is_left_for_the_people_to_accept(self):
        code, out, _ = self.run_prepare(["APPROVE"], "--target", TARGET, "--set", "sync_content=never")
        self.assertIn("These preparation steps failed:", out)
        self.assertIn("[force_config_sync]", out)                    # with sync_to_peer = Both the upgrade run syncs the pair again
        self.assertIn("Running the script accepts these cautions", out)
        self.assertEqual(code, 0)                                    # a kit was written: the pair can still be synced by the upgrade run


class AutomaticJournals(unittest.TestCase):
    """Nobody names a journal: every run gets its own file, nothing is overwritten, an unfinished run is offered back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fleet = self.dir / "fleet.json"
        self.fleet.write_text(json.dumps(fleet_data(pair("A1", "A2"))), encoding="utf-8")
        self.runs = self.dir / "runs"
        code, _, _ = self.cli("plan", "--target", TARGET, "--out", str(self.dir / "plan"))
        template = json.loads((self.dir / "plan" / "approval.template.json").read_text(encoding="utf-8"))
        template["serials"] = ["A1", "A2"]
        template["acks"] = template.pop("cautions_available")
        self.approval = self.dir / "approval.json"
        self.approval.write_text(json.dumps(template), encoding="utf-8")

    def cli(self, verb, *extra):
        from unittest import mock
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):   # never a terminal
            code = cli.main([verb, "--source", f"fake:{self.fleet}", *extra, "--color", "never"])
        return code, out.getvalue(), err.getvalue()

    def upgrade(self, *extra):
        return self.cli("upgrade", "--target", TARGET, "--approval", str(self.approval), "--runs-dir", str(self.runs), *extra)

    def journals(self):
        return sorted(self.runs.glob("upgrade-*.jsonl")) if self.runs.exists() else []

    def test_no_journal_argument_needed_and_each_run_is_a_new_file(self):
        code, out, _ = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("journal: ", out)
        self.assertEqual(len(self.journals()), 1)

    def test_an_unfinished_run_is_offered_back_not_overwritten(self):
        code, _, _ = self.upgrade("--sim-fail", "A2:install")
        self.assertEqual(code, 1)
        first = self.journals()
        self.assertEqual(len(first), 1)
        code, _, err = self.upgrade()                                      # no terminal, no flag: it refuses and says what to add
        self.assertEqual(code, 2)
        self.assertIn("--resume", err)
        self.assertIn("--fresh", err)
        self.assertEqual(self.journals(), first)                           # and nothing was created or touched

    def test_resume_continues_the_same_file_and_fresh_starts_another(self):
        self.upgrade("--sim-fail", "A2:install")
        first = self.journals()
        code, out, _ = self.upgrade("--resume")
        self.assertIn("(continuing)", out)
        self.assertEqual(self.journals(), first)                           # the same file, extended
        code, out, _ = self.upgrade("--fresh")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.journals()), 2)                          # a new file; the old one is kept

    def test_a_finished_run_does_not_get_offered_for_continuation(self):
        self.assertEqual(self.upgrade()[0], 0)
        code, out, _ = self.upgrade()                                      # a later identical plan starts fresh without any fuss
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.journals()), 2)

    def test_resume_with_nothing_to_resume_says_so(self):
        code, _, err = self.upgrade("--resume")
        self.assertEqual(code, 2)
        self.assertIn("no unfinished earlier run", err)

    def test_an_explicit_journal_that_exists_gets_a_helpful_message(self):
        named = self.dir / "mine.jsonl"
        self.assertEqual(self.upgrade("--journal", str(named))[0], 0)
        code, _, err = self.upgrade("--journal", str(named))
        self.assertEqual(code, 2)
        self.assertIn("Add --resume to continue it", err)

    def test_prepare_is_safe_to_repeat_and_names_its_own_files_even_within_one_second(self):
        data = fleet_data(pair("A1", "A2", ha_config_sync=False))
        self.fleet.write_text(json.dumps(data), encoding="utf-8")
        for _ in range(3):
            code, out, _ = self.cli("prepare", "--target", TARGET, "--dry-run", "--set", "sync_to_peer=Both", "--runs-dir", str(self.runs))
            self.assertEqual(code, 0, out)
        self.assertEqual(len(list(self.runs.glob("prepare-dry-*.jsonl"))), 3)


class Console(unittest.TestCase):
    def test_plain_mode_has_no_escape_codes_and_colour_mode_does(self):
        plan, _ = plan_for(fleet_data(pair("A1", "A2")))
        plain = console.render_plan(plan, console.Style(False, False))
        self.assertNotIn("\033", plain)
        self.assertIn("[ OK ]", plain)
        colour = console.render_plan(plan, console.Style(True, False))
        self.assertIn("\033[", colour)

    def test_glyph_follows_outcome_not_step(self):
        out = io.StringIO()
        printer = console.EventPrinter(console.Style(False, False), {"A1": "fw-a"}, out)
        printer({"type": "step", "serial": "A1", "step": "2:install 20.2.9", "state": "ok", "detail": {}})
        printer({"type": "step", "serial": "A1", "step": "3:reboot", "state": "failed", "detail": {"error": "timed out"}})
        text = out.getvalue()
        self.assertIn("[ OK ]", text.splitlines()[0])
        self.assertIn("[FAIL]", text.splitlines()[1])
        self.assertIn("timed out", text)

    def test_no_color_environment_variable_wins(self):
        import os
        old = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(console.Style.detect("always").color)
        finally:
            if old is None:
                del os.environ["NO_COLOR"]
            else:
                os.environ["NO_COLOR"] = old


class KitWriting(unittest.TestCase):
    def args(self, **kw):
        base = dict(source="panorama:192.0.2.1", target="20.2.9", settings=None, creds_file=None, path_rules=None,
                    account="reader", work_account="writer", set=["sync_to_peer=UpgradeOnly"])
        base.update(kw)
        return type("A", (), base)

    def make(self, windows):
        from panupgrade import kit
        data = fleet_data(pair("A1", "A2"))
        p, _ = plan_for(data)
        approval = Approval(p["hash"], ["A1", "A2"], [], "tester", "", "2026-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            made = kit.write(tmp, "STAMP", self.args(), p, approval, "tester", windows=windows, project_dir=tmp)
            return {k: (v.read_text(encoding="utf-8") if hasattr(v, "read_text") else v) for k, v in made.items() if k in ("serials", "approval", "script")}, Path(made["script"]).name

    def test_windows_script_is_a_bat_with_the_exact_command(self):
        files, name = self.make(True)
        self.assertTrue(name.endswith(".bat"))
        self.assertIn("python -m panupgrade upgrade --panorama 192.0.2.1", files["script"])
        self.assertIn("--set sync_to_peer=UpgradeOnly", files["script"])
        self.assertIn("--approval", files["script"])
        self.assertIn("--account reader --work-account writer", files["script"])      # pinned, so the script does not depend on the environment it is run under
        self.assertIn("--creds-file", files["script"])
        self.assertIn("A1   # a1", files["serials"])

    def test_unix_script_is_a_sh_and_executable_text(self):
        files, name = self.make(False)
        self.assertTrue(name.endswith(".sh"))
        self.assertTrue(files["script"].startswith("#!/usr/bin/env sh"))


class InteractiveApproval(unittest.TestCase):
    def ask(self, answer, tty=True, jpath=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli._interactive_approval(console.Style(False, False), "H" * 64, ["A1"], ["A1:force_config_sync"],
                                               ["fw-a (A1)"], jpath, ["[force_config_sync] A1: text"], input_fn=lambda _: answer, tty=tty)
        return result, out.getvalue()

    def test_only_the_word_approves_in_any_case_and_everything_asked_is_on_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp, "j.jsonl"))
            approval, shown = self.ask("APPROVE", jpath=journal)
            self.assertEqual((approval.serials, approval.acks), (["A1"], ["A1:force_config_sync"]))
            self.assertIn("fw-a (A1)", shown)
            self.assertIn("[force_config_sync]", shown)
            self.assertTrue(Path(journal + ".approval.json").exists())      # recorded beside the journal
            self.assertIn("typed APPROVE", approval.approved_by)
        self.assertIsNotNone(self.ask("approve")[0])
        for no in ("yes", "", "y", "approved"):
            self.assertIsNone(self.ask(no)[0], no)

    def test_without_a_terminal_there_is_no_approval(self):
        self.assertIsNone(self.ask("APPROVE", tty=False)[0])


class SelectCommandAndDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fleet_file = self.dir / "fleet.json"
        self.fleet_file.write_text(json.dumps(fleet_data(pair("A1", "A2"), pair("B1", "B2", version="20.2.9"))), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_select_writes_a_list_that_plan_accepts(self):
        listing = self.dir / "list.txt"
        code, out, _ = self.run_cli("select", "--source", f"fake:{self.fleet_file}", "--version-is", "20.2.3", "--out", str(listing))
        self.assertEqual(code, 0)
        self.assertIn("2 of 4 devices match", out)
        self.assertEqual(sorted(cli._serials(type("A", (), {"serial": None, "serials_file": str(listing)}))), ["A1", "A2"])
        code, out, _ = self.run_cli("plan", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--serials-file", str(listing),
                                    "--color", "never")
        self.assertEqual(code, 0)
        self.assertIn("b1 (b1) sim-a 20.2.9 -> 20.2.9 / excluded", out.lower())
        self.assertIn("a1 (a1) sim-a 20.2.3 -> 20.2.9", out.lower())

    def test_a_live_source_with_unreadable_credentials_stops_cleanly_before_any_network_use(self):
        code, _, err = self.run_cli("upgrade", "--panorama", "192.0.2.1", "--creds-file", str(self.dir / "none.txt"),
                                    "--target", TARGET, "--approval", "a.json", "--journal", str(self.dir / "j"))
        self.assertEqual(code, 2)
        self.assertIn("credentials file", err)
        self.assertFalse((self.dir / "j").exists())      # nothing was created, nothing was started

    def test_the_old_auto_approve_flag_is_gone(self):
        with self.assertRaises(SystemExit):
            self.run_cli("upgrade", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--journal", str(self.dir / "j"),
                         "--auto-approve")

    def test_a_dry_run_asks_for_no_approval(self):
        code, out, _ = self.run_cli("upgrade", "--dry-run", "--source", f"fake:{self.fleet_file}", "--target", TARGET,
                                    "--serial", "A1", "--serial", "A2", "--journal", str(self.dir / "dj"), "--color", "never")
        self.assertEqual(code, 0)
        self.assertIn("no approval is asked", out)

    def _prepare_with_typed_answers(self, answers, *extra):
        from unittest import mock

        class Tty(io.StringIO):
            def isatty(self):
                return True
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", Tty()), mock.patch("builtins.input", side_effect=answers),                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["prepare", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--serial", "A1", "--serial", "A2",
                             "--journal", str(self.dir / "prep.jsonl"), "--kit-dir", str(self.dir / "kit"), "--color", "never", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_prepare_writes_the_approval_and_a_script_that_runs_the_upgrade(self):
        code, out, _ = self._prepare_with_typed_answers(["APPROVE"])
        self.assertEqual(code, 0, out)
        kit_files = sorted(p.name for p in (self.dir / "kit").iterdir() if not p.name.startswith("report-"))
        self.assertEqual(sorted(n.rsplit(".", 1)[-1] for n in kit_files), sorted(["json", "txt", ("bat" if __import__("os").name == "nt" else "sh")]))
        approval = next((self.dir / "kit").glob("*.approval.json"))
        script = next((self.dir / "kit").glob("run-*")).read_text(encoding="utf-8")
        self.assertIn("panupgrade upgrade", script)
        self.assertIn(approval.name, script)
        self.assertNotIn("password", script.lower())
        # the unattended upgrade: the approval written at prepare time is all it needs
        code, out, _ = self.run_cli("upgrade", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--serials-file",
                                    str(next((self.dir / "kit").glob("*.serials.txt"))), "--approval", str(approval),
                                    "--journal", str(self.dir / "up.jsonl"), "--color", "never")
        self.assertEqual(code, 0, out)

    def test_there_is_one_prompt_for_the_preparation_and_the_kit_and_report_follow_without_another(self):
        code, out, _ = self._prepare_with_typed_answers(["APPROVE"])          # a second answer would raise: it is never asked for
        self.assertEqual(code, 0, out)
        kit_dir = self.dir / "kit"
        self.assertEqual(sorted(p.suffix for p in kit_dir.glob("report-*")), [".html", ".txt", ".xlsx"])
        self.assertEqual(len(list(kit_dir.glob("run-*"))), 1)
        record = json.loads(next(kit_dir.glob("*.approval.json")).read_text(encoding="utf-8"))
        self.assertIn("by running the generated script", record["approved_by"])
        self.assertIn("running the script is the approval", record["note"])

    def test_declining_the_preparation_writes_no_kit(self):
        code, _, _ = self._prepare_with_typed_answers(["no"])
        self.assertEqual(code, 3)
        self.assertFalse((self.dir / "kit").exists())

    def test_the_prepared_approval_stops_working_when_the_pair_changes_under_it(self):
        self._prepare_with_typed_answers(["APPROVE"])
        approval = next((self.dir / "kit").glob("*.approval.json"))
        data = json.loads(self.fleet_file.read_text(encoding="utf-8"))
        a1, a2 = data["devices"][0], data["devices"][1]
        a1["ha_state"], a2["ha_state"] = "passive", "active"
        a1["facts"]["ha_state"], a2["facts"]["ha_state"] = "passive", "active"          # an admin swapped the roles meanwhile
        self.fleet_file.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = self.run_cli("upgrade", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--serial", "A1", "--serial", "A2",
                                    "--approval", str(approval), "--journal", str(self.dir / "up2.jsonl"), "--color", "never")
        self.assertEqual(code, 3)
        self.assertIn("What is different now", err)
        self.assertIn("HA role active -> passive", err)               # says WHICH firewall and WHAT changed, not just that something did
        self.assertIn("run `panupgrade prepare` again", err.replace("Run `panupgrade", "run `panupgrade"))
        self.assertFalse((self.dir / "up2.jsonl").exists())            # a refused run leaves no journal to trip over next time

    def test_an_old_approval_is_refused(self):
        self._prepare_with_typed_answers(["APPROVE"])
        approval = next((self.dir / "kit").glob("*.approval.json"))
        record = json.loads(approval.read_text(encoding="utf-8"))
        record["created"] = "2026-01-01T00:00:00+00:00"
        approval.write_text(json.dumps(record), encoding="utf-8")
        code, _, err = self.run_cli("upgrade", "--source", f"fake:{self.fleet_file}", "--target", TARGET, "--serial", "A1", "--serial", "A2",
                                    "--approval", str(approval), "--journal", str(self.dir / "up3.jsonl"), "--color", "never")
        self.assertEqual(code, 3)
        self.assertIn("hours old", err)

    def test_snapshot_reads_only_the_included_devices(self):
        class Spy(FakeFleet):
            asked = []
            def facts(self, serial):
                self.asked.append(serial)
                return super().facts(serial)
        spy = Spy(json.loads(self.fleet_file.read_text(encoding="utf-8")))
        data = snapshot(spy, {"A1", "A2"})
        self.assertEqual(sorted(spy.asked), ["A1", "A2"])
        self.assertEqual({d["serial"]: bool(d["facts"]) for d in data["devices"]}, {"A1": True, "A2": True, "B1": False, "B2": False})

    def test_snapshot_round_trips_through_the_fleet_reader(self):
        original = FakeFleet(json.loads(self.fleet_file.read_text(encoding="utf-8")))
        again = FakeFleet(snapshot(original))
        self.assertEqual([r.serial for r in again.list_devices()], [r.serial for r in original.list_devices()])
        self.assertEqual(again.facts("A1").ha_state, "active")


if __name__ == "__main__":
    unittest.main()
