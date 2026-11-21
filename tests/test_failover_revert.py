"""A session count that cannot be read is not a count of zero, and a failed fail-over puts the traffic back where it was known to work."""

import types
import unittest

from tests import test_execution as te
from tests.support import fleet_data, pair
from panupgrade import executor
from panupgrade.executor import BackendError
from panupgrade.simulator import SimBackend


class Silent(SimBackend):
    """The upgraded member does not answer a session count question while the old active member is suspended (what a read through Panorama
    does right after a hand-over)."""

    def session_count(self, serial):
        if serial == "A2" and self.nodes["A1"].state == "suspended":
            raise BackendError("the read operation timed out")
        return super().session_count(serial)


class WhenTheCountCannotBeRead(te.Base):
    def test_the_traffic_is_put_back_on_the_old_active_member_and_the_log_says_so(self):
        data = fleet_data(pair("A1", "A2"))
        sim = Silent(data)
        _, _, _, journal, result = self.go(data, backend=sim)
        state, why = result["outcome"]["A1"]
        self.assertEqual(state, "failed")
        self.assertIn("could not be read", why)
        self.assertIn("traffic moving to it cannot be confirmed", why)
        self.assertNotIn("0 sessions", why)                                     # unreadable is not zero
        self.assertIn("reverted", why)
        self.assertIn("A1 is active, A2 is passive", why)
        self.assertEqual((sim.nodes["A1"].state, sim.nodes["A2"].state), ("active", "passive"))
        order = [c[:2] for c in sim.calls if c[:2] in (("A1", "suspend"), ("A1", "functional"), ("A2", "suspend"), ("A2", "functional"))]
        self.assertEqual(order[:4], [("A1", "suspend"), ("A1", "functional"), ("A2", "suspend"), ("A2", "functional")])
        notes = [e["detail"]["message"] for e in journal.steps_of("A1") if e["state"] == "note"]
        self.assertTrue([n for n in notes if "the session count could not be read" in n and "timed out" in n])
        self.assertTrue([n for n in notes if n.startswith("revert: ")])

    def test_a_real_collapse_is_reverted_the_same_way(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1)
        _, _, _, _, result = self.go(data, backend=sim)
        self.assertIn("90% short", result["outcome"]["A1"][1])
        self.assertEqual((sim.nodes["A1"].state, sim.nodes["A2"].state), ("active", "passive"))

    def test_a_revert_that_cannot_hand_the_role_back_says_who_is_active(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1, fail={("A2", "suspend"): "boom"})
        _, _, _, _, result = self.go(data, backend=sim)
        why = result["outcome"]["A1"][1]
        self.assertIn("could not suspend A2 (boom)", why)
        self.assertIn("A1 is passive, A2 is active", why)
        self.assertIn("by hand", why)

    def test_a_revert_that_cannot_make_the_old_member_functional_stops_there(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1, fail={("A1", "functional"): "boom"})
        _, _, _, _, result = self.go(data, backend=sim)
        self.assertIn("could not make A1 functional (boom)", result["outcome"]["A1"][1])
        self.assertNotIn(("A2", "suspend"), [c[:2] for c in sim.calls])


class WhenTheOldActiveMembersOwnCountCannotBeReadAtTheFailOver(te.Base):
    def test_the_count_recorded_when_the_unit_started_is_used_and_the_check_still_runs(self):
        class Dim(SimBackend):
            def state(self, serial):
                got = super().state(serial)
                if serial == "A1" and got["ha_state"] == "active" and got["version"] == "20.2.3" and ("A2", "reboot") in [c[:2] for c in self.calls]:
                    got = dict(got, session_count=None)
                return got
        data = fleet_data(pair("A1", "A2"))
        sim = Dim(data, takeover_ratio=0.1)
        _, _, _, _, result = self.go(data, backend=sim)
        why = result["outcome"]["A1"][1]
        self.assertIn("90% short", why)                       # the check ran against the recorded baseline instead of being skipped
        self.assertIn("reverted", why)


class TheWatch(unittest.TestCase):
    class Journal:
        def __init__(self):
            self.notes = []

        def record(self, serial, key, state, detail=None):
            self.notes.append(detail["message"])

    settings = types.SimpleNamespace(session_noise_floor=100, poll_interval_s=15)

    def test_an_unreadable_count_is_unknown_not_zero(self):
        backend, journal, errors = types.SimpleNamespace(real_time=False), self.Journal(), ["timed out"]
        level, mine, _, best, _ = executor._watch_sessions(backend, journal, "A2", "k", lambda: (None, [1000]), self.settings, 0.35, 0.8, 60, "x", errors)
        self.assertEqual((level, mine, best), ("unknown", None, None))
        self.assertIn("timed out", journal.notes[0])

    def test_it_keeps_asking_until_the_wait_is_up(self):
        clock, asked = {"now": 0.0}, []

        def observe():
            asked.append(clock["now"])
            return None, [1000]

        def nap(seconds):
            clock["now"] += seconds
        real = executor.time
        executor.time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=nap)
        try:
            level = executor._watch_sessions(types.SimpleNamespace(real_time=True), self.Journal(), "A2", "k", observe, self.settings, 0.35, 0.8, 60, "x")[0]
        finally:
            executor.time = real
        self.assertEqual(level, "unknown")
        self.assertEqual(asked, [0.0, 15.0, 30.0, 45.0, 60.0])

    def test_a_count_that_arrives_late_is_judged_on_its_value(self):
        clock, answers = {"now": 0.0}, [None, None, 950]

        def nap(seconds):
            clock["now"] += seconds
        real = executor.time
        executor.time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=nap)
        try:
            level, mine = executor._watch_sessions(types.SimpleNamespace(real_time=True), self.Journal(), "A2", "k",
                                                   lambda: (answers.pop(0), [1000]), self.settings, 0.35, 0.8, 60, "x")[:2]
        finally:
            executor.time = real
        self.assertEqual((level, mine), ("ok", 950))

    def test_zero_read_successfully_is_still_zero(self):
        level, mine = executor._watch_sessions(types.SimpleNamespace(real_time=False), self.Journal(), "A2", "k", lambda: (0, [1000]),
                                               self.settings, 0.35, 0.8, 60, "x")[:2]
        self.assertEqual((level, mine), ("fail", 0))

    def test_sessions_returns_none_for_a_failed_read_and_keeps_what_it_said(self):
        class Be:
            def session_count(self, serial):
                raise BackendError("no answer")
        errors = []
        self.assertIsNone(executor._sessions(Be(), "A2", errors))
        self.assertEqual(errors, ["no answer"])


if __name__ == "__main__":
    unittest.main()
