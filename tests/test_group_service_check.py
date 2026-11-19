"""A stand-alone group member is not finished until its session table is back: warn band, fail band, and the wait before the next member."""

import tempfile
import time
import types
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, device, fleet_data, pair
from panupgrade import console, executor, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet

GROUP = {"S1": "edge", "S2": "edge", "S3": "edge"}


def three():
    return fleet_data(device("S1"), device("S2"), device("S3"))      # 1000 sessions each


def plan_for(data, groups=GROUP, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, Selection(groups=groups), rules=TEST_RULES), s


def approve(plan):
    return Approval(plan["hash"], [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
                    [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])


class InThePlan(unittest.TestCase):
    def test_each_group_member_ends_with_the_service_check_and_nothing_else_has_one(self):
        plan, _ = plan_for(three())
        for d in plan["devices"]:
            self.assertEqual(d["steps"][-1]["action"], "check service")
            self.assertEqual(d["group_peers"], sorted({"S1", "S2", "S3"} - {d["serial"]}))
        plan, _ = plan_for(fleet_data(pair("A1", "A2")), groups={})
        self.assertNotIn("check service", [s["action"] for d in plan["devices"] for s in d["steps"]])

    def test_the_bands_are_settings_with_the_agreed_defaults_and_part_of_the_plan(self):
        s = settings.load(None, [])
        self.assertEqual((s.group_session_warn_band, s.group_session_fail_band, s.group_session_wait_s), (0.35, 0.80, 600))
        self.assertNotEqual(plan_for(three())[0]["hash"], plan_for(three(), group_session_warn_band="0.2")[0]["hash"])

    def test_a_warn_band_above_the_fail_band_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["group_session_warn_band=0.9", "group_session_fail_band=0.5"])


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = 0

    def go(self, backend, data, **opts):
        self.runs += 1
        self.jpath = str(Path(self._tmp.name, f"run{self.runs}.jsonl"))
        plan, s = plan_for(data, **opts)
        s.poll_interval_s, s.settle_after_failover_s, s.settle_after_reboot_s = 1, 0, 0
        journal = Journal.create(self.jpath, plan["hash"])
        return journal, executor.run(plan, approve(plan), backend, journal, s)


class AfterTheReboot(SimBackend):
    """S1 comes back with ``after`` sessions (what its table holds once it has rebooted)."""
    after = 1000

    def reboot_and_wait(self, serial, timeout_s):
        version = super().reboot_and_wait(serial, timeout_s)
        if serial == "S1":
            self.nodes[serial].sessions = self.after
        return version


def backend(after):
    class B(AfterTheReboot):
        pass
    B.after = after
    return B(three())


class Judged(Base):
    def test_a_member_whose_table_is_back_passes_quietly(self):
        journal, result = self.go(backend(1000), three())
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(journal.detail("S1", "group_baseline"), {"sessions": 1000})

    def test_a_loss_inside_the_warn_band_passes_without_a_warning(self):
        journal, result = self.go(backend(700), three())                       # 30% short, band 35%
        self.assertEqual(result["outcome"]["S1"][0], "done")
        self.assertFalse([e for e in journal.steps_of("S1") if e["detail"].get("warning")])

    def test_a_loss_between_the_bands_is_a_warning_and_the_run_goes_on(self):
        journal, result = self.go(backend(500), three())                       # 50% short: warn, not fail
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        warned = [e for e in journal.steps_of("S1") if e["detail"].get("warning")]
        self.assertEqual(len(warned), 1)
        self.assertIn("50% short", warned[0]["detail"]["warning"])
        kind, text = console.describe_event(warned[0], {})
        self.assertEqual(kind, "warn")

    def test_a_collapse_is_a_failure_that_stops_the_rest_of_the_group(self):
        journal, result = self.go(backend(150), three())                       # 85% short
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertIn("not carrying traffic", result["outcome"]["S1"][1])

    def test_the_bands_are_the_users_to_move(self):
        _, result = self.go(backend(500), three(), group_session_warn_band="0.6")        # 50% short is now inside the warn band
        self.assertEqual(result["outcome"]["S1"][0], "done")
        _, result = self.go(backend(500), three(), group_session_warn_band="0.3", group_session_fail_band="0.4")
        self.assertEqual(result["outcome"]["S1"][0], "failed")

    def test_the_other_members_average_counts_when_the_member_is_closer_to_it(self):
        data = fleet_data(device("S1"), device("S2"), device("S3"))
        data["devices"][0]["facts"]["session_count"] = 3000               # S1 was carrying three times the others
        journal, result = self.go(backend(1000), data)                     # now 1000 = the others' average, 67% short of its own
        self.assertEqual(result["outcome"]["S1"][0], "done")
        self.assertFalse([e for e in journal.steps_of("S1") if e["detail"].get("warning")])


class Waiting(Base):
    """A member that is slow to take traffic back is waited for, and the next member is not touched meanwhile."""

    def test_it_waits_until_inside_the_band_and_the_next_member_does_not_start_before(self):
        clock = {"now": 0.0}
        reads = {"n": 0}

        class Slow(SimBackend):
            real_time = True

            def state(self, serial):
                got = super().state(serial)
                if serial == "S1" and any(c[:2] == ("S1", "reboot") for c in self.calls):
                    reads["n"] += 1
                    mine = next(i for i, c in enumerate(self.calls) if c[:2] == ("S1", "reboot"))
                    intruded = any(c[1] == "reboot" and c[0] != "S1" for c in self.calls[mine:])     # another member started rebooting meanwhile
                    recovered = reads["n"] >= 6 or intruded
                    if recovered and ("S1", "recovered") not in self.calls:
                        self.calls.append(("S1", "recovered"))
                    got = dict(got, session_count=950 if recovered else 100)                  # back on the sixth look
                return got

            def reboot_and_wait(self, serial, timeout_s):
                return super().reboot_and_wait(serial, timeout_s)

        sim = Slow(three())
        def nap(seconds):                                   # the clock moves on at once; a real moment passes so other threads get their turn
            clock["now"] += max(seconds, 1)
            time.sleep(0.03)
        fake = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=nap)
        real = executor.time
        executor.time = fake
        try:
            journal, result = self.go(sim, three(), max_parallel="3")
        finally:
            executor.time = real
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        notes = [e for e in journal.steps_of("S1") if e["state"] == "note" and "waiting up to" in e["detail"].get("message", "")]
        self.assertEqual(len(notes), 1)
        calls = sim.calls
        first_drain = next(i for i, c in enumerate(calls) if c[0] == "S1" and c[1] == "reboot")
        last_read = calls.index(("S1", "recovered"))
        for other in ("S2", "S3"):
            mine = [i for i, c in enumerate(calls) if c[0] == other and c[1] == "reboot"]
            self.assertTrue(all(i < first_drain or i > last_read for i in mine), (other, mine, first_drain, last_read))

    def test_still_short_when_the_wait_runs_out_is_judged_by_the_bands(self):
        clock = {"now": 0.0}

        class Stuck(SimBackend):
            real_time = True

            def state(self, serial):
                got = super().state(serial)
                if serial == "S1" and any(c[:2] == ("S1", "reboot") for c in self.calls):
                    got = dict(got, session_count=100)                                    # 90% short for ever
                return got

        sim = Stuck(three())
        fake = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + 100))
        real = executor.time
        executor.time = fake
        try:
            _, result = self.go(sim, three(), group_session_wait_s="300")
        finally:
            executor.time = real
        self.assertEqual(result["outcome"]["S1"][0], "failed")
        self.assertIn("after 300s", result["outcome"]["S1"][1])


if __name__ == "__main__":
    unittest.main()
