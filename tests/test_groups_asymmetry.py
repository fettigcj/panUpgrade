"""Stand-alone groups (a reboot tolerance) and the asymmetry window (recorded, set, restored, reported), against the simulator."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, device, fleet_data, pair
from panupgrade import cli, executor, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet
from panupgrade.windows import Gate, unrestored_in_journal

ORIGINAL = {"reject_non_syn": True, "reject_non_syn_config": None, "asymmetric_path": None}
TOLERANT = {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "bypass"}


def plan_for(data, groups=None, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, Selection(groups=groups or {}), rules=TEST_RULES), s


def approve(plan):
    return Approval(plan["hash"], [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
                    [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])


def three():
    return fleet_data(device("S1"), device("S2"), device("S3"))


GROUP = {"S1": "edge", "S2": "edge", "S3": "edge"}


class Planning(unittest.TestCase):
    def test_a_group_member_is_not_held_back_as_a_stand_alone_outage_and_its_reboot_is_covered(self):
        plan, _ = plan_for(three(), GROUP)
        self.assertEqual({d["verdict"] for d in plan["devices"]}, {"ready"})
        self.assertEqual(plan["groups"], {"edge": ["S1", "S2", "S3"]})
        reboots = {s["traffic"] for d in plan["devices"] for s in d["steps"] if s["action"] == "reboot"}
        self.assertEqual(reboots, {"redundancy"})

    def test_without_a_group_a_stand_alone_firewall_still_needs_the_outage_to_be_accepted(self):
        plan, _ = plan_for(three())
        self.assertEqual({d["verdict"] for d in plan["devices"]}, {"skipped"})

    def test_an_ha_member_in_a_group_is_blocked_with_the_reason(self):
        plan, _ = plan_for(fleet_data(pair("A1", "A2")), {"A1": "edge"})
        a1 = next(d for d in plan["devices"] if d["serial"] == "A1")
        self.assertEqual(a1["verdict"], "blocked")
        self.assertIn("HA member", " ".join(r["reason"] for r in a1["results"]))

    def test_the_window_is_in_the_plan_and_needs_an_acknowledgement_when_it_is_on(self):
        plan, _ = plan_for(three(), GROUP)
        self.assertEqual(plan["asymmetry"], [{"kind": "group", "name": "edge", "serials": ["S1", "S2", "S3"]}])
        self.assertIn("asymmetry_window", [c["code"] for c in plan["cautions"]])

    def test_group_window_off_means_no_window_and_no_caution(self):
        plan, _ = plan_for(three(), GROUP, group_tolerate_asymmetry="false")
        self.assertEqual(plan["asymmetry"], [])
        self.assertNotIn("asymmetry_window", [c["code"] for c in plan["cautions"]])

    def test_pairs_are_left_alone_by_default_and_get_a_window_when_asked(self):
        data = fleet_data(pair("A1", "A2"))
        self.assertEqual(plan_for(data)[0]["asymmetry"], [])
        window = plan_for(data, pair_tolerate_asymmetry="true")[0]["asymmetry"]
        self.assertEqual([(w["kind"], w["serials"]) for w in window], [("pair", ["A2", "A1"])])

    def test_what_the_group_is_and_the_window_are_part_of_what_is_approved(self):
        one, _ = plan_for(three(), GROUP)
        other, _ = plan_for(three(), {**GROUP, "S3": "other"})
        off, _ = plan_for(three(), GROUP, group_tolerate_asymmetry="false")
        self.assertNotEqual(one["hash"], other["hash"])
        self.assertNotEqual(one["hash"], off["hash"])


class TheListFile(unittest.TestCase):
    def test_a_serial_may_carry_a_group_after_a_comma_and_a_plain_serial_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "list.txt")
            path.write_text("# note\nS1, edge   # fw one\nS2,edge west\nS3   # fw three\n", encoding="utf-8")
            self.assertEqual(cli._read_list(path), ["S1", "S2", "S3"])
            self.assertEqual(cli._read_groups(path), {"S1": "edge", "S2": "edge west"})

    def test_writing_and_reading_back_keeps_the_groups(self):
        class Ref:
            def __init__(self, serial):
                self.serial, self.hostname, self.sw_version, self.ha_state = serial, serial.lower(), "10.2.1", None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "list.txt")
            cli._write_list(path, [Ref("S1"), Ref("S2")], "by hand", {"S1": "edge"})
            self.assertEqual(cli._read_groups(path), {"S1": "edge"})
            self.assertEqual(cli._read_list(path), ["S1", "S2"])


class Running(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jpath = str(Path(self._tmp.name, "run.jsonl"))

    def go(self, backend_class=SimBackend, data=None, groups=GROUP, fail=None, **opts):
        data = data or three()
        plan, s = plan_for(data, groups, **opts)
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        backend = backend_class(data, fail=fail)
        journal = Journal.create(self.jpath, plan["hash"])
        return plan, backend, journal, executor.run(plan, approve(plan), backend, journal, s)

    def ops(self, backend, name):
        return [i for i, call in enumerate(backend.calls) if call[1] == name]

    def test_the_window_opens_before_any_member_reboots_and_closes_after_the_last_one_finished(self):
        _, sim, journal, result = self.go()
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        sets, reboots, verifies = self.ops(sim, "set asymmetry"), self.ops(sim, "reboot"), self.ops(sim, "state")
        self.assertEqual(len(sets), 6)                                          # three opened, three restored
        self.assertTrue(all(sim.calls[i][2:] == (False, "bypass", "no") for i in sets[:3]))
        self.assertLess(sets[2], reboots[0])
        self.assertGreater(sets[3], reboots[-1])
        self.assertGreater(sets[3], verifies[-1] - 100)
        for serial in ("S1", "S2", "S3"):
            self.assertEqual(sim.nodes[serial].asym, ORIGINAL)
        self.assertEqual(result["asymmetry_unrestored"], [])

    def test_every_change_and_restore_is_in_the_journal_with_the_original_values(self):
        _, _, journal, _ = self.go()
        for serial in ("S1", "S2", "S3"):
            steps = [(e["step"], e["state"]) for e in journal.steps_of(serial) if e["step"].startswith("asymmetry")]
            self.assertEqual(steps, [("asymmetry open", "started"), ("asymmetry open", "ok"), ("asymmetry restore", "started"), ("asymmetry restore", "ok")])
            self.assertEqual(journal.detail(serial, "asymmetry open")["original"], ORIGINAL)
        self.assertEqual(unrestored_in_journal(journal, ["S1", "S2", "S3"]), [])

    def test_the_firewalls_own_values_are_what_comes_back_not_the_defaults(self):
        own = {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "drop"}

        class Tuned(SimBackend):
            def __init__(self, data, fail=None):
                super().__init__(data, fail=fail)
                self.nodes["S1"].asym = dict(own)
        _, sim, journal, _ = self.go(Tuned)
        self.assertEqual(sim.nodes["S1"].asym, own)
        self.assertEqual(journal.detail("S1", "asymmetry open")["original"], own)

    def test_the_window_is_closed_even_when_a_member_fails(self):
        _, sim, journal, result = self.go(fail={("S2", "install"): "boom"})
        self.assertEqual(result["outcome"]["S2"][0], "failed")
        for serial in ("S1", "S2", "S3"):
            self.assertEqual(sim.nodes[serial].asym, ORIGINAL)
        self.assertIn(("asymmetry restore", "ok"), [(e["step"], e["state"]) for e in journal.steps_of("S2")])

    def test_a_restore_that_fails_is_reported_and_stays_in_the_journal(self):
        class RefusesToRestore(SimBackend):
            def set_tcp_asymmetry(self, serial, reject_non_syn, reject_non_syn_config, asymmetric_path):
                if serial == "S2" and reject_non_syn:                           # the restore of S2 only
                    self.calls.append((serial, "set asymmetry", reject_non_syn, asymmetric_path, reject_non_syn_config))
                    raise executor.BackendError("S2: the commit was refused")
                super().set_tcp_asymmetry(serial, reject_non_syn, reject_non_syn_config, asymmetric_path)
        _, sim, journal, result = self.go(RefusesToRestore)
        self.assertEqual([e["serial"] for e in result["asymmetry_unrestored"]], ["S2"])
        self.assertEqual(result["asymmetry_unrestored"][0]["original"], ORIGINAL)
        self.assertEqual(sim.nodes["S1"].asym, ORIGINAL)
        self.assertEqual(sim.nodes["S2"].asym, TOLERANT)                         # still changed: that is exactly what the alert is for
        self.assertEqual([s for s in unrestored_in_journal(journal, ["S1", "S2", "S3"])][0]["serial"], "S2")
        self.assertIn(("asymmetry restore", "failed"), [(e["step"], e["state"]) for e in journal.steps_of("S2")])

    def test_a_firewall_with_uncommitted_changes_is_refused_before_anything_is_changed(self):
        data = three()
        data["devices"][1]["facts"]["pending_changes"] = True
        _, sim, _, result = self.go(data=data, pending_changes="ignore")
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"held"})
        self.assertIn("uncommitted", result["outcome"]["S1"][1])
        for serial in ("S1", "S2", "S3"):
            self.assertEqual(sim.nodes[serial].asym, ORIGINAL)
        self.assertEqual(self.ops(sim, "reboot"), [])

    def test_a_window_that_cannot_open_on_one_member_puts_back_the_ones_it_had_changed(self):
        _, sim, journal, result = self.go(fail={("S3", "set asymmetry"): "refused"})
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"held"})
        for serial in ("S1", "S2", "S3"):
            self.assertEqual(sim.nodes[serial].asym, ORIGINAL)
        self.assertEqual(result["asymmetry_unrestored"], [])

    def test_no_window_is_touched_when_the_setting_is_off(self):
        _, sim, _, result = self.go(group_tolerate_asymmetry="false")
        self.assertEqual(self.ops(sim, "set asymmetry"), [])
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})

    def test_a_pair_gets_the_same_treatment_when_asked(self):
        data = fleet_data(pair("A1", "A2"))
        _, sim, journal, result = self.go(data=data, groups={}, pair_tolerate_asymmetry="true")
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(len(self.ops(sim, "set asymmetry")), 4)
        self.assertLess(self.ops(sim, "set asymmetry")[1], self.ops(sim, "suspend")[0])
        for serial in ("A1", "A2"):
            self.assertEqual(sim.nodes[serial].asym, ORIGINAL)


class TheRebootTolerance(unittest.TestCase):
    def run_group(self, tolerance, members=3, **fail):
        data = fleet_data(*[device(f"S{i}") for i in range(1, members + 1)])
        groups = {f"S{i}": "edge" for i in range(1, members + 1)}
        seen = {"now": 0, "most": 0}
        lock = threading.Lock()

        class Slow(SimBackend):
            def reboot_and_wait(self, serial, timeout_s):
                with lock:
                    seen["now"] += 1
                    seen["most"] = max(seen["most"], seen["now"])
                time.sleep(0.05)
                try:
                    return super().reboot_and_wait(serial, timeout_s)
                finally:
                    with lock:
                        seen["now"] -= 1
        plan, s = plan_for(data, groups, group_reboot_tolerance=str(tolerance), group_tolerate_asymmetry="false", max_parallel="5")
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal.create(str(Path(tmp, "j.jsonl")), plan["hash"])
            sim = Slow(data, fail=fail.get("fail"))
            result = executor.run(plan, approve(plan), sim, journal, s)
        return result, seen, sim

    def test_by_default_one_member_of_a_group_reboots_at_a_time(self):
        result, seen, _ = self.run_group(1)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(seen["most"], 1)

    def test_a_larger_tolerance_lets_that_many_reboot_together_and_no_more(self):
        result, seen, _ = self.run_group(2, members=5)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(seen["most"], 2)

    def test_a_member_that_fails_out_of_service_keeps_its_slot_and_the_rest_of_the_group_is_not_started(self):
        data = fleet_data(*[device(f"S{i}") for i in range(1, 4)])
        plan, s = plan_for(data, GROUP, group_tolerate_asymmetry="false", max_parallel="1")
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        sim = SimBackend(data, fail={("S1", "reboot"): "never came back"})
        journal = Journal.create(self.id_path(), plan["hash"])
        result = executor.run(plan, approve(plan), sim, journal, s)
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertEqual([result["outcome"][s_][0] for s_ in ("S2", "S3")], ["held", "held"])
        self.assertIn("s1 failed", result["outcome"]["S2"][1])
        self.assertEqual(sim.nodes["S2"].version, "20.2.3")

    def id_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return str(Path(tmp.name, "j.jsonl"))


class TheGate(unittest.TestCase):
    def test_it_hands_out_only_as_many_slots_as_the_tolerance(self):
        gate = Gate(2)
        self.assertEqual((gate.enter("g"), gate.enter("g")), ("", ""))
        got = []
        waiter = threading.Thread(target=lambda: got.append(gate.enter("g")))
        waiter.start()
        time.sleep(0.1)
        self.assertEqual(got, [])
        gate.leave("g")
        waiter.join(3)
        self.assertEqual(got, [""])

    def test_a_closed_group_refuses_everyone_waiting_and_stopping_ends_the_wait(self):
        gate = Gate(1)
        gate.enter("g")
        gate.close("g", "fw-1")
        self.assertIn("fw-1 failed", gate.enter("g"))
        stop = threading.Event()
        stop.set()
        other = Gate(1)
        other.enter("x")
        self.assertIn("stopped", other.enter("x", stop))


class TheAlert(unittest.TestCase):
    def test_a_firewall_that_was_not_put_back_is_named_with_the_exact_commands_to_fix_it(self):
        from panupgrade import console
        entry = {"serial": "S2", "window": "group edge", "original": {"reject_non_syn": True, "reject_non_syn_config": None, "asymmetric_path": None}, "error": "the commit was refused"}
        text = chr(10).join(console.unrestored_lines([entry], {"S2": "fw-two"}, console.Style()))
        self.assertIn("ALERT", text)
        self.assertIn("fw-two (S2)", text)
        self.assertIn("set session tcp-reject-non-syn yes", text)
        self.assertIn("delete deviceconfig setting tcp asymmetric-path", text)
        self.assertIn("the commit was refused", text)
        entry["original"] = {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "drop"}
        text = chr(10).join(console.unrestored_lines([entry], {}, console.Style()))
        self.assertIn("tcp-reject-non-syn no", text)
        self.assertIn("setting session tcp-reject-non-syn no", text)
        self.assertIn("asymmetric-path drop", text)
        self.assertEqual(console.unrestored_lines([], {}, console.Style()), [])


class TheLoadBalancerDrain(unittest.TestCase):
    """lb_drain_profile: before a group member reboots the probe's protocol is taken out of the named profile, and it is put back, however the member ends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jpath = str(Path(self._tmp.name, "run.jsonl"))

    def go(self, backend_class=SimBackend, fail=None, groups=GROUP, **opts):
        opts = {"lb_drain_profile": "LB-Probe", "group_tolerate_asymmetry": "false", **opts}
        data = three()
        plan, s = plan_for(data, groups, **opts)
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 0, 0, 0
        backend = backend_class(data, fail=fail)
        journal = Journal.create(self.jpath, plan["hash"])
        return plan, backend, journal, executor.run(plan, approve(plan), backend, journal, s)

    def test_the_plan_drains_after_the_install_and_undrains_after_the_last_verify_and_asks_for_an_acknowledgement(self):
        plan, _ = plan_for(three(), GROUP, lb_drain_profile="LB-Probe")
        actions = [s["action"] for s in plan["devices"][0]["steps"]]
        self.assertLess(actions.index("install " + TARGET), actions.index("drain"))
        self.assertLess(actions.index("drain"), actions.index("reboot"))
        self.assertEqual(actions[-2:], ["verify", "undrain"])
        self.assertIn("lb_drain", [c["code"] for c in plan["cautions"]])

    def test_nothing_is_planned_without_a_profile_and_a_pair_or_ungrouped_firewall_is_never_drained(self):
        plan, _ = plan_for(three(), GROUP)
        self.assertNotIn("drain", [s["action"] for d in plan["devices"] for s in d["steps"]])
        plan, _ = plan_for(fleet_data(pair("A1", "A2")), lb_drain_profile="LB-Probe")
        self.assertNotIn("drain", [s["action"] for d in plan["devices"] for s in d["steps"]])

    def test_the_profile_is_drained_while_the_member_reboots_and_back_afterwards(self):
        seen = {}

        class Watching(SimBackend):
            def reboot_and_wait(self, serial, timeout_s):
                seen[serial] = dict(self.nodes[serial].profiles["LB-Probe"])
                return super().reboot_and_wait(serial, timeout_s)
        _, sim, journal, result = self.go(Watching)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        for serial in ("S1", "S2", "S3"):
            self.assertEqual(seen[serial], {"ssh": "no", "ping": "yes"})             # only the probe's protocol was touched
            self.assertEqual(sim.nodes[serial].profiles["LB-Probe"], {"ssh": "yes", "ping": "yes"})
            states = [(e["step"], e["state"]) for e in journal.steps_of(serial) if e["step"].startswith("asymmetry lb")]
            self.assertEqual(states, [("asymmetry lb drain", "started"), ("asymmetry lb drain", "ok"), ("asymmetry lb restore", "started"), ("asymmetry lb restore", "ok")])
        self.assertEqual(result["lb_unrestored"], [])

    def test_the_probe_protocol_setting_chooses_what_is_taken_out(self):
        seen = {}

        class Watching(SimBackend):
            def reboot_and_wait(self, serial, timeout_s):
                seen[serial] = dict(self.nodes[serial].profiles["LB-Probe"])
                return super().reboot_and_wait(serial, timeout_s)

        class Both(Watching):
            def __init__(self, data, fail=None):
                super().__init__(data, fail=fail)
                for node in self.nodes.values():
                    node.profiles["LB-Probe"] = {"ssh": "yes", "http": "yes", "ping": "yes"}
        _, sim, _, _ = self.go(Both, lb_drain_probe_protocol="ssh+http")
        self.assertEqual(seen["S1"], {"ssh": "no", "http": "no", "ping": "yes"})
        self.assertEqual(sim.nodes["S1"].profiles["LB-Probe"], {"ssh": "yes", "http": "yes", "ping": "yes"})

    def test_a_profile_only_a_template_holds_gets_a_local_copy_for_the_drain_and_the_copy_is_deleted_afterwards(self):
        seen = {}

        class Templated(SimBackend):
            def __init__(self, data, fail=None):
                super().__init__(data, fail=fail)
                for node in self.nodes.values():
                    node.template_profiles["LB-Probe"] = node.profiles.pop("LB-Probe")

            def reboot_and_wait(self, serial, timeout_s):
                seen[serial] = dict(self.nodes[serial].profiles.get("LB-Probe", {}))
                return super().reboot_and_wait(serial, timeout_s)
        _, sim, _, result = self.go(Templated)
        self.assertEqual(seen["S1"], {"ssh": "no", "ping": "yes"})                # drained through a local copy
        for serial in ("S1", "S2", "S3"):
            self.assertNotIn("LB-Probe", sim.nodes[serial].profiles)               # no local copy left: the template resumes
            self.assertEqual(sim.nodes[serial].template_profiles["LB-Probe"], {"ssh": "yes", "ping": "yes"})
        self.assertEqual(result["lb_unrestored"], [])

    def test_a_member_that_fails_after_being_drained_has_its_profile_put_back_and_the_group_stops(self):
        plan, sim, journal, result = self.go(fail={("S1", "reboot"): "never came back"}, max_parallel="1")
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertEqual(sim.nodes["S1"].profiles["LB-Probe"], {"ssh": "yes", "ping": "yes"})
        self.assertEqual(result["lb_unrestored"], [])
        self.assertEqual([result["outcome"][s][0] for s in ("S2", "S3")], ["held", "held"])

    def test_the_failed_member_is_put_back_at_once_not_only_by_the_end_of_run_safety_net(self):
        from unittest import mock
        from panupgrade.windows import Windows
        with mock.patch.object(Windows, "close_all", lambda self: None):          # without the end-of-run sweep, only the member's own cleanup is left
            _, sim, _, result = self.go(fail={("S1", "reboot"): "never came back"}, max_parallel="1")
        self.assertEqual(sim.nodes["S1"].profiles["LB-Probe"], {"ssh": "yes", "ping": "yes"})
        self.assertEqual(result["lb_unrestored"], [])

    def test_a_profile_that_cannot_be_put_back_is_an_alert_with_what_it_should_hold(self):
        class Stuck(SimBackend):
            def set_lb_profile(self, serial, profile, changes):
                if serial == "S2" and "yes" in changes.values():
                    self.calls.append((serial, "set profile", profile))
                    raise executor.BackendError("S2: no answer")
                super().set_lb_profile(serial, profile, changes)
        _, sim, journal, result = self.go(Stuck)
        self.assertEqual([e["serial"] for e in result["lb_unrestored"]], ["S2"])
        self.assertEqual(result["lb_unrestored"][0]["original"]["ssh"], "yes")
        self.assertIn("no answer", result["lb_unrestored"][0]["error"])
        self.assertEqual(result["outcome"]["S2"][0], "failed")                      # the undrain step is part of the member's own steps
        from panupgrade import console
        text = chr(10).join(console.lb_unrestored_lines(result["lb_unrestored"], {"S2": "fw-two"}, console.Style()))
        self.assertIn("ALERT", text)
        self.assertIn("fw-two (S2)", text)
        self.assertIn("ssh yes", text)

    def test_a_missing_profile_fails_that_member_before_anything_is_changed(self):
        _, sim, _, result = self.go(lb_drain_profile="No-Such-Profile", max_parallel="1")
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertIn("not on this firewall", result["outcome"]["S1"][1])
        self.assertEqual(sim.nodes["S1"].profiles["LB-Probe"], {"ssh": "yes", "ping": "yes"})
        self.assertEqual(self.reboots(sim), [])

    def reboots(self, sim):
        return [c for c in sim.calls if c[1] == "reboot"]

    def test_a_member_holds_its_group_slot_from_the_drain_to_the_undrain(self):
        state = {"now": 0, "most": 0}
        lock = threading.Lock()

        class Counting(SimBackend):
            def set_lb_profile(self, serial, profile, changes):
                with lock:
                    state["now"] += 1 if "no" in changes.values() else -1
                    state["most"] = max(state["most"], state["now"])
                time.sleep(0.02)
                super().set_lb_profile(serial, profile, changes)
        _, _, _, result = self.go(Counting, max_parallel="5")
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(state["most"], 1)

    def test_the_probe_setting_and_profile_name_are_checked(self):
        self.assertEqual(settings.load(None, ["lb_drain_probe_protocol=SSH+HTTP"]).lb_drain_probe_protocol, "ssh+http")
        for bad in ("lb_drain_probe_protocol=telnet", "lb_drain_profile=a;b", "lb_drain_wait_s=-1"):
            with self.assertRaises(settings.SettingsError, msg=bad):
                settings.load(None, [bad])


if __name__ == "__main__":
    unittest.main()
