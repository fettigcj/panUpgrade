"""HA pairs: warn and fail bands on the session table, judged where the traffic is carried, with a wait for the table to come back."""

import time
import types
import unittest

from tests import test_execution as te
from tests.support import fleet_data, pair
from panupgrade import console, executor
from panupgrade.simulator import SimBackend


def warnings(journal, serial):
    return [e for e in journal.steps_of(serial) if e["detail"].get("warning")]


class AtTheFailOver(te.Base):
    def test_a_table_between_the_bands_is_a_warning_and_the_upgrade_goes_on(self):
        data = fleet_data(pair("A1", "A2"))
        _, _, _, journal, result = self.go(data, backend=SimBackend(data, takeover_ratio=0.5))        # 50% short
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        found = [e for e in warnings(journal, "A1") if "fail over" in e["step"]]     # the end-of-pair check, later, sees the same short table and warns too
        self.assertEqual(len(found), 1)
        self.assertIn("50% short", found[0]["detail"]["warning"])
        self.assertEqual(console.describe_event(found[0], {})[0], "warn")

    def test_a_collapse_puts_the_member_back_and_says_what_the_pair_is_left_on(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1)                                                       # 90% short
        _, _, _, journal, result = self.go(data, backend=sim)
        state, why = result["outcome"]["A1"]
        self.assertEqual(state, "failed")
        self.assertIn("reverted", why)
        self.assertIn("two versions", why)                      # passive first: A2 is upgraded and A1 is not
        self.assertIn(("A1", "functional"), [c[:2] for c in sim.calls])
        # lifting the suspension is not enough: the upgraded peer is suspended so that A1 carries the traffic again, then let back in
        order = [c[:2] for c in sim.calls if c[:2] in (("A1", "functional"), ("A2", "suspend"), ("A2", "functional"))]
        self.assertEqual(order[-3:], [("A1", "functional"), ("A2", "suspend"), ("A2", "functional")])
        self.assertEqual((sim.nodes["A1"].state, sim.nodes["A2"].state), ("active", "passive"))
        self.assertIn("A1 is active, A2 is passive", why)

    def test_the_active_first_order_leaves_both_on_the_old_version_when_it_collapses(self):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data, takeover_ratio=0.1)
        _, _, _, _, result = self.go(data, backend=sim, upgrade_first="active")
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertNotIn("two versions", result["outcome"]["A1"][1])
        self.assertEqual(result["outcome"]["A2"][0], "held")
        self.assertEqual((sim.nodes["A1"].version, sim.nodes["A2"].version), ("20.2.3", "20.2.3"))

    def test_a_table_that_is_slow_to_arrive_is_waited_for(self):
        data = fleet_data(pair("A1", "A2"))
        clock, reads = {"now": 0.0}, {"n": 0}

        class Slow(SimBackend):
            real_time = True

            def state(self, serial):
                got = super().state(serial)
                if serial == "A2" and any(c[:2] == ("A1", "suspend") for c in self.calls) and got["ha_state"] == "active" and got["version"] == "20.2.3" or \
                        (serial == "A2" and got["ha_state"] == "active" and not any(c[:2] == ("A1", "reboot") for c in self.calls)):
                    reads["n"] += 1
                    got = dict(got, session_count=50 if reads["n"] < 4 else got["session_count"])
                return got

        def nap(seconds):
            clock["now"] += max(seconds, 1)
            time.sleep(0.001)
        real = executor.time
        executor.time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=nap)
        try:
            _, _, _, journal, result = self.go(data, backend=Slow(data))
        finally:
            executor.time = real
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        notes = [e for e in journal.steps_of("A1") if e["state"] == "note" and "waiting up to" in e["detail"].get("message", "")]
        self.assertGreaterEqual(len(notes), 1)


class WhenTheLastMemberIsDone(te.Base):
    def finished_with(self, kept):
        data = fleet_data(pair("A1", "A2"))
        sim = SimBackend(data)
        real = sim.state

        def state(serial):
            got = real(serial)
            if any(c[:2] == ("A2", "reboot") for c in sim.calls) and got["ha_state"] == "active":
                got = dict(got, session_count=int(1000 * kept))
            return got
        sim.state = state
        return self.go(data, backend=sim)

    def test_a_table_between_the_bands_is_a_warning(self):
        _, _, _, journal, result = self.finished_with(0.6)                       # 40% short
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(len(warnings(journal, "A1")), 1)

    def test_a_collapse_fails_the_last_member(self):
        _, _, _, _, result = self.finished_with(0.1)
        self.assertEqual(result["outcome"]["A1"][0], "failed")
        self.assertIn("failure at 80%", result["outcome"]["A1"][1])

    def test_inside_the_warn_band_says_nothing(self):
        _, _, _, journal, result = self.finished_with(0.8)
        self.assertEqual({v[0] for v in result["outcome"].values()}, {"done"})
        self.assertEqual(warnings(journal, "A1"), [])


class TheSettings(unittest.TestCase):
    def test_the_old_names_say_what_replaced_them(self):
        from panupgrade import settings
        for old in ("failover_band=0.3", "post_upgrade_band=0.5"):
            with self.assertRaises(settings.SettingsError) as caught:
                settings.load(None, [old])
            self.assertIn("pair_session_warn_band", str(caught.exception))

    def test_the_warn_band_may_not_pass_the_fail_band(self):
        from panupgrade import settings
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["pair_session_warn_band=0.9", "pair_session_fail_band=0.5"])


if __name__ == "__main__":
    unittest.main()
