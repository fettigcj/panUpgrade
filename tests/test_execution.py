"""Phase 2: approval gate, executor, journal/resume, all against the simulator. No network anywhere."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, device, facts, fleet, fleet_data, pair
from panupgrade import executor, settings
from panupgrade.approval import Approval
from panupgrade.executor import Backend, ExecutionRefused
from panupgrade.journal import Journal, JournalError
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet

T = TARGET


def make(data, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    plan = build_plan(FakeFleet(data), T, s, Selection(accept_standalone_outage=True), rules=TEST_RULES)
    return plan, s


def approve(plan, serials=None, acks=True):
    serials = serials if serials is not None else [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")]
    return Approval(plan["hash"], serials, [f"{c['scope']}:{c['code']}" for c in plan["cautions"]] if acks else [])


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jpath = str(Path(self._tmp.name, "run.jsonl"))

    def go(self, data, backend=None, serials=None, acks=True, **opts):
        plan, s = make(data, **opts)
        backend = backend or SimBackend(data)
        journal = Journal.create(self.jpath, plan["hash"])
        return plan, s, backend, journal, executor.run(plan, approve(plan, serials, acks), backend, journal, s)


class WaitForPassive(Base):
    def test_a_pair_runs_start_to_finish_with_the_checkpoint_recorded_in_the_journal(self):
        data = fleet_data(pair("A1", "A2"))
        plan, s, backend, journal, result = self.go(data)
        self.assertEqual(result["failures"], 0)
        entries = [e for e in journal.steps_of("A1") if "wait for the passive firewall's upgrade to finish" in e["step"] and e["state"] == "ok"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"]["peer_version"], str(TARGET))

    def test_it_refuses_to_hand_over_to_a_peer_that_is_not_on_the_version_this_member_is_going_to(self):
        class Peer:
            def state(self, serial):
                return {"version": "10.1.1"}
        steps = [{"action": "install 20.2.9", "traffic": "none", "note": ""}, {"action": "wait for the passive firewall's upgrade to finish", "traffic": "none", "note": ""}]
        dev = {"serial": "A1", "peer_serial": "A2", "steps": steps}
        with self.assertRaisesRegex(executor.StepFailed, "refusing to hand over"):
            executor._do("wait for the passive firewall's upgrade to finish", 1, dev, Peer(), None, None)

    def test_with_nothing_to_install_there_is_nothing_to_compare_and_it_passes(self):
        class Peer:
            def state(self, serial):
                return {"version": "10.1.1"}
        dev = {"serial": "A1", "peer_serial": "A2", "steps": [{"action": "wait for the passive firewall's upgrade to finish", "traffic": "none", "note": ""}]}
        self.assertEqual(executor._do("wait for the passive firewall's upgrade to finish", 0, dev, Peer(), None, None), {"peer_version": "10.1.1"})


class ApprovalGate(Base):
    def test_nothing_runs_without_a_matching_complete_approval(self):
        data = fleet_data(pair("A1", "A2"), device("S1", version="20.1.4"))
        plan, s = make(data)
        journal = Journal.create(self.jpath, plan["hash"])
        sim = SimBackend(data)
        bad = {
            "wrong plan": Approval("0" * 64, ["A1", "A2"]),
            "no devices": Approval(plan["hash"], []),
            "unknown device": Approval(plan["hash"], ["ZZZ"]),
        }
        for label, approval in bad.items():
            with self.assertRaises(ExecutionRefused, msg=label):
                executor.run(plan, approval, sim, journal, s)
        self.assertEqual(sim.calls, [])

    def test_unacknowledged_caution_refuses(self):
        data = fleet_data(pair("A1", "A2"))
        s = settings.load()
        plan = build_plan(FakeFleet(data), T, s, Selection(serials=["A1"]), rules=TEST_RULES)   # active alone: caution
        self.assertTrue(plan["cautions"])
        journal = Journal.create(self.jpath, plan["hash"])
        sim = SimBackend(data)
        with self.assertRaises(ExecutionRefused) as ctx:
            executor.run(plan, approve(plan, acks=False), sim, journal, s)
        self.assertIn("unacknowledged caution", str(ctx.exception))
        self.assertEqual(sim.calls, [])
        executor.run(plan, approve(plan, acks=True), sim, journal, s)     # acknowledged: now it runs

    def test_blocked_device_cannot_be_approved(self):
        data = fleet_data(device("B1", facts=facts(pending_changes=True)))
        plan, s = make(data)
        with self.assertRaises(ExecutionRefused) as ctx:
            executor.run(plan, Approval(plan["hash"], ["B1"]), SimBackend(data), Journal.create(self.jpath, plan["hash"]), s)
        self.assertIn("blocked", str(ctx.exception))

    def test_journal_for_another_plan_is_refused(self):
        data = fleet_data(pair("A1", "A2"))
        plan, s = make(data)
        other = Journal.create(self.jpath, "f" * 64)
        with self.assertRaises(ExecutionRefused):
            executor.run(plan, approve(plan), SimBackend(data), other, s)


class HappyPath(Base):
    def test_pair_is_upgraded_passive_first_and_judged_on_its_own_version(self):
        data = fleet_data(pair("A1", "A2"))
        plan, s, sim, journal, result = self.go(data)
        self.assertEqual({k: v[0] for k, v in result["outcome"].items()}, {"A2": "done", "A1": "done"})
        self.assertEqual({n.serial: n.version for n in sim.nodes.values()}, {"A1": T, "A2": T})
        first_install = [c[0] for c in sim.calls if c[1] == "install"]
        self.assertEqual(first_install, ["A2", "A1"])
        self.assertEqual(sim.nodes["A1"].state, "passive")   # went live again, not left suspended
        self.assertEqual(sim.nodes["A2"].state, "active")

    def test_backup_precedes_every_install_in_the_journal(self):
        data = fleet_data(pair("A1", "A2"))
        _, _, _, journal, _ = self.go(data)
        for serial in ("A1", "A2"):
            ok = [e["step"] for e in journal.steps_of(serial) if e["state"] == "ok"]
            self.assertLess(next(i for i, k in enumerate(ok) if k.endswith(":backup")),
                            next(i for i, k in enumerate(ok) if ":install" in k))

    def test_preemption_member_is_held_suspended_through_the_upgrade(self):
        data = fleet_data(pair("A1", "A2", preemption=True))
        _, _, sim, journal, result = self.go(data)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        # A2 was suspended before its install, and A1 is active again after preemption at go live
        ops = [(c[0], c[1]) for c in sim.calls]
        self.assertLess(ops.index(("A2", "suspend")), ops.index(("A2", "install")))
        self.assertEqual(sim.nodes["A1"].state, "active")

    def test_restore_roles_makes_the_original_active_active_again(self):
        data = fleet_data(pair("A1", "A2"))
        _, _, sim, _, result = self.go(data, restore_roles="restore")
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual((sim.nodes["A1"].state, sim.nodes["A2"].state), ("active", "passive"))

    def test_install_failure_is_recorded_and_holds_the_partner(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, fail={("A2", "install"): "disk full during install"})
        _, _, _, journal, result = self.go(data, backend=sim)
        self.assertEqual(result["outcome"]["A2"][0], "failed")
        self.assertIn("disk full during install", result["outcome"]["A2"][1])
        self.assertEqual(result["outcome"]["A1"][0], "held")
        self.assertEqual(result["failures"], 1)
        failed = [e for e in journal.steps_of("A2") if e["state"] == "failed"]
        self.assertEqual(failed[0]["detail"]["error"], "disk full during install")
        self.assertNotIn(("A1", "install", T), [c[:3] for c in sim.calls])

    def test_backup_failure_stops_the_device_before_any_change(self):
        data = fleet_data(device("S1"))
        sim = SimBackend(data, fail={("S1", "backup"): "export refused"})
        _, _, _, _, result = self.go(data, backend=sim)
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertFalse([c for c in sim.calls if c[1] in ("install", "reboot")])

    def test_unverified_backup_is_a_failure(self):
        class Unparseable(SimBackend):
            def backup(self, serial):
                super().backup(serial)
                return {"sha256": "x", "bytes": 10, "parsed_ok": False}

        data = fleet_data(device("S1"))
        sim = Unparseable(data)
        _, _, _, _, result = self.go(data, backend=sim)
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertIn("not verified", result["outcome"]["S1"][1])
        self.assertFalse([c for c in sim.calls if c[1] in ("install", "reboot")])

    def test_install_and_reboot_refuse_without_a_journaled_backup(self):
        data = fleet_data(device("S1"))
        plan, s = make(data)
        dev = plan["devices"][0]
        dev["steps"] = [st for st in dev["steps"] if st["action"] != "backup"]
        journal = Journal.create(self.jpath, plan["hash"])
        sim = SimBackend(data)
        state, detail = executor._run_device(dev, sim, journal, s)
        self.assertEqual(state, "failed")
        self.assertIn("no verified backup", detail)
        self.assertFalse([c for c in sim.calls if c[1] in ("install", "reboot")])

    def test_a_device_is_judged_on_its_own_version_not_the_previous_one(self):
        data = fleet_data(device("S1"), device("S2"))
        sim = SimBackend(data, wrong_version={"S2": "20.2.3"})   # S2 silently stays on the old version
        _, _, _, _, result = self.go(data, backend=sim)
        self.assertEqual(result["outcome"]["S1"][0], "done")
        self.assertEqual(result["outcome"]["S2"][0], "failed")
        self.assertIn("expected " + T, result["outcome"]["S2"][1])

    def test_stale_plan_or_pending_config_found_at_execution_time_blocks(self):
        data = fleet_data(device("S1"), device("S2"))
        plan, s = make(data)
        live = fleet_data(device("S1", version="20.2.4"), device("S2"))
        live["devices"][1]["facts"]["pending_changes"] = True
        _, _, _, result = None, None, None, executor.run(plan, approve(plan), SimBackend(live),
                                                          Journal.create(self.jpath, plan["hash"]), s)
        self.assertIn("plan is stale", result["outcome"]["S1"][1])
        self.assertIn("pending", result["outcome"]["S2"][1])

    def test_stops_starting_new_devices_after_max_failures(self):
        data = fleet_data(device("S1"), device("S2"), device("S3"), device("S4"))
        sim = SimBackend(data, fail={("S1", "install"): "x", ("S2", "install"): "y"})
        _, _, _, _, result = self.go(data, backend=sim, max_failures=2)
        states = {k: v[0] for k, v in result["outcome"].items()}
        self.assertEqual(states, {"S1": "failed", "S2": "failed", "S3": "not_started", "S4": "not_started"})
        self.assertTrue(result["stopped"])
        self.assertFalse([c for c in sim.calls if c[0] in ("S3", "S4")])

    def test_failover_outside_the_session_band_is_reverted(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.4)    # the new active holds 40% of the old sessions
        _, _, _, _, result = self.go(data, backend=sim, max_failures=5)
        self.assertEqual(result["outcome"]["A2"][0], "done")
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("reverted", result["outcome"]["A1"][1])
        self.assertEqual(sim.nodes["A1"].version, "20.2.3")      # never upgraded

    def test_session_band_is_tunable(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.8)
        _, _, _, _, result = self.go(data, backend=sim, failover_band=0.3)
        self.assertEqual(result["outcome"]["A1"][0], "done")


class Resume(Base):
    def test_resume_skips_finished_work_and_never_retries_a_failed_device(self):
        data = fleet_data(device("S1"), device("S2"), device("S3"))
        sim1 = SimBackend(data, fail={("S2", "install"): "boom"})
        plan, s = make(data)
        journal = Journal.create(self.jpath, plan["hash"])
        first = executor.run(plan, approve(plan), sim1, journal, s)
        self.assertEqual({k: v[0] for k, v in first["outcome"].items()}, {"S1": "done", "S2": "failed", "S3": "done"})

        sim2 = SimBackend(data)       # fresh devices that would succeed, if asked
        again = executor.run(plan, approve(plan), sim2, Journal.resume(self.jpath, plan["hash"]), s)
        self.assertEqual(again["outcome"]["S2"][0], "failed")
        self.assertEqual(again["failures"], 1)
        self.assertEqual([c for c in sim2.calls if c[1] in ("install", "reboot", "backup")], [])

    def test_interrupted_run_resumes_after_the_last_finished_step(self):
        data = fleet_data(device("S1"))
        plan, s = make(data)
        journal = Journal.create(self.jpath, plan["hash"])
        steps = plan["devices"][0]["steps"]
        stop_at = next(i for i, st in enumerate(steps) if st["action"].startswith("install"))
        journal.record("S1", "preflight", "ok")
        for i, st in enumerate(steps[:stop_at]):
            journal.record("S1", f"{i}:{st['action']}", "started")
            journal.record("S1", f"{i}:{st['action']}", "ok", {"session_baseline": 1000} if st["action"] == "backup" else {})
        sim = SimBackend(data)
        sim.nodes["S1"].downloaded.add(T)
        result = executor.run(plan, approve(plan), sim, Journal.resume(self.jpath, plan["hash"]), s)
        self.assertEqual(result["outcome"]["S1"][0], "done")
        self.assertEqual([c[1] for c in sim.calls if c[1] in ("download", "backup", "preflight")], [])
        self.assertIn("install", [c[1] for c in sim.calls])

    def test_in_doubt_step_is_never_replayed(self):
        data = fleet_data(device("S1"))
        plan, s = make(data)
        journal = Journal.create(self.jpath, plan["hash"])
        steps = plan["devices"][0]["steps"]
        journal.record("S1", "preflight", "ok")
        for i, st in enumerate(steps):
            journal.record("S1", f"{i}:{st['action']}", "started")
            if st["action"] == "reboot":
                break
            journal.record("S1", f"{i}:{st['action']}", "ok", {"session_baseline": 1000} if st["action"] == "backup" else {})
        sim = SimBackend(data)
        result = executor.run(plan, approve(plan), sim, Journal.resume(self.jpath, plan["hash"]), s)
        self.assertEqual(result["outcome"]["S1"][0], "needs_attention")
        self.assertEqual(sim.calls, [])

    def test_resume_against_a_different_plan_is_refused(self):
        data = fleet_data(device("S1"))
        plan, _ = make(data)
        Journal.create(self.jpath, plan["hash"])
        with self.assertRaises(JournalError):
            Journal.resume(self.jpath, "e" * 64)

    def test_torn_final_line_is_tolerated_but_mid_file_corruption_is_not(self):
        Journal.create(self.jpath, "h").record("S1", "preflight", "ok")
        with open(self.jpath, "a", encoding="utf-8") as handle:
            handle.write('{"type": "step", "serial": "S1", "ste')
        self.assertEqual(len(Journal.resume(self.jpath, "h").events), 1)
        with open(self.jpath, "a", encoding="utf-8") as handle:
            handle.write('\n{"type": "step", "serial": "S1", "step": "x", "state": "ok", "detail": {}}\n')
        with self.assertRaises(JournalError):
            Journal.resume(self.jpath, "h")

    def test_create_refuses_to_overwrite_an_existing_journal(self):
        Journal.create(self.jpath, "h")
        with self.assertRaises(JournalError):
            Journal.create(self.jpath, "h")


class Boundaries(unittest.TestCase):
    def test_only_the_simulator_and_the_allow_listed_live_backend_exist(self):
        from panupgrade import simulator  # noqa: F401  (import registers the subclass)
        self.assertLessEqual({c.__name__ for c in Backend.__subclasses__()}, {"SimBackend", "LiveBackend"})

    def test_approval_file_roundtrip_ignores_the_template_help_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "a.json")
            path.write_text(json.dumps({"plan_hash": "h", "serials": ["A1"], "acks": [], "cautions_available": ["x:y"]}),
                            encoding="utf-8")
            self.assertEqual(Approval.from_file(path).serials, ["A1"])
            path.write_text(json.dumps({"plan_hash": "h", "serials": [], "bogus": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Approval.from_file(path)


class Cli(unittest.TestCase):
    def test_upgrade_refuses_a_bad_approval_and_runs_a_good_one(self):
        import contextlib
        import io
        from panupgrade import cli
        with tempfile.TemporaryDirectory() as d:
            fleet_file, rules_file = Path(d, "fleet.json"), Path(d, "rules.json")
            fleet_file.write_text(json.dumps(fleet_data(pair("A1", "A2"))), encoding="utf-8")
            rules_file.write_text(json.dumps(TEST_RULES), encoding="utf-8")
            common = ["--source", f"fake:{fleet_file}", "--target", T, "--path-rules", str(rules_file)]
            out = Path(d, "out")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["plan", *common, "--out", str(out)]), 0)
            template = json.loads(Path(out, "approval.template.json").read_text(encoding="utf-8"))
            bad = Path(d, "bad.json")
            bad.write_text(json.dumps({**template, "serials": []}), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["upgrade", *common, "--approval", str(bad), "--journal", str(Path(d, "j0"))]), 3)
            good = Path(d, "good.json")
            good.write_text(json.dumps({**template, "serials": ["A1", "A2"], "approved_by": "test"}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(cli.main(["upgrade", *common, "--approval", str(good), "--journal", str(Path(d, "j1"))]), 0)
            self.assertIn("SIMULATION", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
