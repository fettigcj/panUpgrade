"""An installed image waiting for a reboot: the three cases the plan understands, and the one it hands back to the user."""

import unittest

from tests.support import TARGET, TEST_RULES, device, facts, ha_facts, fleet_data
from panupgrade import settings
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet


def plan(data, groups=None, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    return build_plan(FakeFleet(data), TARGET, s, Selection(accept_standalone_outage=True, groups=groups or {}), rules=TEST_RULES)


def dev(plan_, serial):
    return next(d for d in plan_["devices"] if d["serial"] == serial)


def pending(serial, running, version, **over):
    return device(serial, version=running, facts=facts(sw_version=running, pending_reboot=True, pending_reboot_version=version, **over))


def actions(d):
    return [s["action"] for s in d["steps"]]


class PendingReboot(unittest.TestCase):
    def test_the_target_is_already_installed_so_the_upgrade_is_only_the_reboot(self):
        p = plan(fleet_data(pending("A", "20.2.3", TARGET)))
        d = dev(p, "A")
        self.assertEqual(d["verdict"], "caution")
        self.assertEqual(d["activate"], {"version": TARGET, "case": "finish"})
        self.assertEqual(actions(d), ["backup", f"reboot to activate {TARGET}", "verify"])
        self.assertEqual(d["steps"][1]["traffic"], "outage")                 # a stand-alone firewall: the reboot is an outage
        self.assertTrue(any(c["code"] == "pending_reboot" for c in p["cautions"]))

    def test_low_disk_does_not_hold_a_firewall_that_only_needs_its_reboot(self):
        d = dev(plan(fleet_data(pending("A", "20.2.3", TARGET, disk_free_mb=100))), "A")
        self.assertEqual(d["verdict"], "caution")

    def test_a_pair_member_pending_the_version_its_peer_runs_reboots_into_it_then_goes_on_to_the_target(self):
        a = device("A", version="20.2.3", facts=ha_facts("passive", "B", sw_version="20.2.3", pending_reboot=True, pending_reboot_version="20.2.5"))
        b = device("B", version="20.2.5", facts=ha_facts("active", "A", sw_version="20.2.5"))
        a["peer_serial"], b["peer_serial"] = "B", "A"
        d = dev(plan(fleet_data(a, b)), "A")
        self.assertEqual(d["activate"]["case"], "match")
        acts = actions(d)
        self.assertLess(acts.index("reboot to activate 20.2.5"), next(i for i, x in enumerate(acts) if x.startswith("install ")))
        self.assertEqual(acts[acts.index("reboot to activate 20.2.5") + 1], "verify")

    def test_a_group_member_pending_the_version_the_rest_of_its_group_runs_is_brought_level_first(self):
        data = fleet_data(pending("A", "20.2.3", "20.2.5"), device("B", version="20.2.5"), device("C", version="20.2.5"))
        p = plan(data, groups={"A": "g", "B": "g", "C": "g"}, group_tolerate_asymmetry="false")
        self.assertEqual(dev(p, "A")["activate"]["case"], "match")

    def test_anything_else_is_handed_back_with_what_to_run_first(self):
        p = plan(fleet_data(pending("A", "20.2.3", "20.2.5")))
        d = dev(p, "A")
        self.assertEqual(d["verdict"], "blocked")
        self.assertNotIn("activate", d)
        text = " ".join(r["reason"] for r in d["results"] if r["check"] == "pending_reboot")
        self.assertIn("clean-up batch", text)
        self.assertIn("20.2.5", text)

    def test_a_group_that_is_not_all_on_the_pending_version_is_handed_back(self):
        data = fleet_data(pending("A", "20.2.3", "20.2.5"), device("B", version="20.2.5"), device("C", version="20.2.3"))
        p = plan(data, groups={"A": "g", "B": "g", "C": "g"}, group_tolerate_asymmetry="false")
        d = dev(p, "A")
        self.assertEqual(d["verdict"], "blocked")
        self.assertIn("the rest of its group", " ".join(r["reason"] for r in d["results"] if r["check"] == "pending_reboot"))

    def test_the_asymmetry_window_does_not_block_a_pending_firewall_it_opens_there_after_the_reboot(self):
        data = fleet_data(pending("A", "20.2.3", TARGET), device("B", version="20.2.3"))
        p = plan(data, groups={"A": "g", "B": "g"})                          # group_tolerate_asymmetry is on by default
        d = dev(p, "A")
        self.assertEqual(d["verdict"], "caution")
        self.assertEqual(d["activate"]["case"], "finish")
        self.assertIn("after that reboot", " ".join(r["reason"] for r in d["results"] if r["check"] == "pending_reboot"))

    def test_a_group_member_waiting_for_its_reboot_goes_first(self):
        data = fleet_data(device("B", version="20.2.3"), pending("A", "20.2.3", TARGET), device("C", version="20.2.3"))
        p = plan(data, groups={"A": "g", "B": "g", "C": "g"})
        self.assertEqual(p["order"][0], ["A"])

    def test_a_member_waiting_for_its_reboot_is_not_drained_before_that_reboot_because_a_drain_is_a_commit(self):
        data = fleet_data(pending("A", "20.2.3", TARGET), device("B", version="20.2.3"))
        p = plan(data, groups={"A": "g", "B": "g"}, lb_drain_profile="LB")
        self.assertEqual([a for a in actions(dev(p, "A")) if a in ("drain", "reboot to activate " + TARGET)], ["reboot to activate " + TARGET])
        self.assertIn("drain", actions(dev(p, "B")))

    def test_a_downgrade_is_drained_before_the_older_image_is_installed_because_it_refuses_commits_after(self):
        data = fleet_data(device("A", version="21.0.1"), device("B", version="21.0.1"))
        p = plan(data, groups={"A": "g", "B": "g"}, lb_drain_profile="LB", allow_downgrade="true")
        acts = actions(dev(p, "A"))
        self.assertLess(acts.index("drain"), next(i for i, x in enumerate(acts) if x.startswith("install ")))
        up = plan(fleet_data(device("A", version="20.2.1"), device("B", version="20.2.1")), groups={"A": "g", "B": "g"}, lb_drain_profile="LB")
        acts = actions(dev(up, "A"))
        self.assertGreater(acts.index("drain"), next(i for i, x in enumerate(acts) if x.startswith("install ")))      # an upgrade is unchanged

    def test_a_firewall_with_nothing_pending_is_planned_as_before(self):
        d = dev(plan(fleet_data(device("A", version="20.2.3"))), "A")
        self.assertNotIn("activate", d)
        self.assertFalse(any(x.startswith("reboot to activate") for x in actions(d)))

    def test_the_executor_lets_the_planned_activation_through_preflight_but_not_a_different_image(self):
        from panupgrade import executor

        class Seen:
            def __init__(self, version): self.version = version
            def preflight(self, serial):
                return {"version": "20.2.3", "pending_changes": False, "running_jobs": 0, "pending_reboot": True, "pending_reboot_version": self.version}
        planned = {"serial": "A", "current_version": "20.2.3", "activate": {"version": TARGET, "case": "finish"}}
        self.assertEqual(executor._preflight(planned, Seen(TARGET), settings.load())["version"], "20.2.3")
        with self.assertRaises(executor.StepFailed):
            executor._preflight(planned, Seen("20.2.5"), settings.load())
        with self.assertRaises(executor.StepFailed):
            executor._preflight({"serial": "A", "current_version": "20.2.3"}, Seen(TARGET), settings.load())

    def test_preflight_waits_for_a_job_that_finishes_by_itself_and_gives_up_after_busy_wait_s(self):
        from panupgrade import executor

        class Jobs:
            def __init__(self, counts): self.counts = list(counts)
            def preflight(self, serial):
                return {"version": "20.2.3", "pending_changes": False, "running_jobs": self.counts[0] if len(self.counts) == 1 else self.counts.pop(0)}
            def running_jobs(self, serial):
                return self.counts[0] if len(self.counts) == 1 else self.counts.pop(0)
        s = settings.load(None, ["poll_interval_s=1", "busy_wait_s=1"])
        planned = {"serial": "A", "current_version": "20.2.3"}
        self.assertEqual(executor._preflight(planned, Jobs([3, 1, 0, 0]), s)["version"], "20.2.3")      # busy at first, quiet a moment later
        with self.assertRaises(executor.StepFailed) as stuck:
            executor._preflight(planned, Jobs([2]), s)
        self.assertIn("job(s) in its queue after 1s", str(stuck.exception))

    def test_verify_after_the_activation_expects_the_activated_version(self):
        from panupgrade import executor
        steps = [{"action": "backup"}, {"action": "reboot to activate 20.2.5"}, {"action": "verify"}, {"action": "install 20.2.9"}, {"action": "reboot"}, {"action": "verify"}]
        self.assertEqual(executor._expected_version(steps, 2), "20.2.5")
        self.assertEqual(executor._expected_version(steps, 5), "20.2.9")


if __name__ == "__main__":
    unittest.main()


class NotPlannedAndColour(unittest.TestCase):
    def setUp(self):
        from tests.support import pair
        data = fleet_data(device("OUT", version="20.2.3"), device("SOLO", version="20.2.3"), device("DONE", version=TARGET),
                          pending("BAD", "20.2.3", "20.2.5"), device("OK", version="20.2.3"))
        s = settings.load()
        self.plan = build_plan(FakeFleet(data), TARGET, s, Selection(serials=["SOLO", "DONE", "BAD", "OK"], groups={"OK": "g", "BAD": "g"}), rules=TEST_RULES)

    def test_not_planned_firewalls_are_one_heading_per_reason_with_one_line_each(self):
        from panupgrade import report
        groups = {heading: [d["serial"] for d, _ in rows] for heading, rows in report.not_planned_groups(self.plan)}
        self.assertEqual(groups["Not in the selection"], ["OUT"])
        self.assertEqual(groups["Stand-alone firewalls left out (setting accept_standalone_outage is off)"], ["SOLO"])
        self.assertEqual(groups["Nothing to upgrade: already on the target, or newer with downgrades off"], ["DONE"])
        text = report.to_text(self.plan)
        self.assertIn("Not planned - Not in the selection (1)", text)
        self.assertNotIn("selection: not in the selection", text)             # the per-firewall detail is gone

    def test_a_blocked_firewall_is_red_all_the_way_along_its_line(self):
        from panupgrade import console
        out = console.render_plan(self.plan, console.Style(color=True, emoji=False))
        head = next(line for line in out.split("\n") if "BAD" in line and "->" in line)
        self.assertIn("\033[1;91m", head)
        self.assertIn("\033[0m\033[1;91m", head)                                 # the bold word's reset is followed by red again
        self.assertTrue(head.rstrip().endswith("\033[0m"))
        self.assertIn("\033[1;91mBlocked", out)

    def test_the_html_has_the_blocked_section_and_the_grouped_list(self):
        from panupgrade import report
        html = report.to_html(self.plan)
        self.assertIn('class="h-blocked"', html)
        self.assertIn("Not planned - Not in the selection (1)", html)
