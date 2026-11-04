import unittest

from tests.support import TARGET, TEST_RULES, device, fleet, pair
from panupgrade import settings
from panupgrade.planner import PlanError, Selection, build_plan


def plan(src, sel=None, rules=TEST_RULES, **over):
    return build_plan(src, TARGET, settings.from_dict(over), sel, rules)


def dev(p, serial):
    return next(d for d in p["devices"] if d["serial"] == serial)


def actions(d):
    return [s["action"] for s in d["steps"]]


class StagesWhileThePlanIsBuilt(unittest.TestCase):
    def test_each_firewall_is_announced_as_it_is_checked(self):
        from panupgrade import progress
        heard = []
        progress.listen(lambda text: None, heard.append)
        try:
            plan(fleet(pair("A1", "A2"), device("S1", version="20.1.4")))
        finally:
            progress.listen(None)
        self.assertEqual([h for h in heard if h.startswith("Checking")],
                         ["Checking firewall 1 of 3: a1", "Checking firewall 2 of 3: a2", "Checking firewall 3 of 3: s1"])
        reads = [h.split(":")[0] for h in heard if h.startswith("Reading")]       # read side by side, so the order they finish in is not fixed
        self.assertEqual(sorted(reads), ["Reading firewall 1 of 3", "Reading firewall 2 of 3", "Reading firewall 3 of 3"])

    def test_only_the_selected_firewalls_are_counted(self):
        from panupgrade import progress
        heard = []
        progress.listen(lambda text: None, heard.append)
        try:
            plan(fleet(pair("A1", "A2"), device("S1", version="20.1.4")), Selection(serials=["S1"]))
        finally:
            progress.listen(None)
        self.assertEqual(heard, ["Reading firewall 1 of 1: s1", "Checking firewall 1 of 1: s1"])

    def test_reading_a_snapshot_says_how_far_it_has_got(self):
        from panupgrade import progress
        from panupgrade.sources import snapshot
        heard = []
        progress.listen(lambda text: None, heard.append)
        try:
            snapshot(fleet(pair("A1", "A2")), {"A1", "A2"})
        finally:
            progress.listen(None)
        self.assertEqual([h.split(":")[0] for h in heard],
                         ["Asking Panorama for the list of firewalls", "Reading firewall 1 of 2", "Reading firewall 2 of 2", "Reading Panorama's version"])
        self.assertEqual({h.split(": ")[1] for h in heard if h.startswith("Reading firewall")}, {"a1", "a2"})


class HappyPaths(unittest.TestCase):
    def test_pair_is_one_unit_passive_first(self):
        p = plan(fleet(pair("A1", "A2")))
        self.assertEqual(p["order"], [["A2", "A1"]])
        self.assertEqual((dev(p, "A1")["verdict"], dev(p, "A2")["verdict"]), ("ready", "ready"))
        self.assertIn("fail over", actions(dev(p, "A1")))
        self.assertNotIn("fail over", actions(dev(p, "A2")))
        self.assertTrue(p["mode"].startswith("REPORT ONLY"))

    def test_the_active_member_waits_for_the_passive_one_and_says_so_before_it_fails_over(self):
        p = plan(fleet(pair("A1", "A2")))
        active, passive = actions(dev(p, "A1")), actions(dev(p, "A2"))
        self.assertIn("wait for the passive firewall's upgrade to finish", active)
        self.assertLess(active.index("wait for the passive firewall's upgrade to finish"), active.index("fail over"))
        self.assertNotIn("wait for the passive firewall's upgrade to finish", passive)                       # the passive goes first and waits for nobody
        wait_step = next(s for s in dev(p, "A1")["steps"] if s["action"] == "wait for the passive firewall's upgrade to finish")
        self.assertIn("a2 (the passive member)", wait_step["note"])
        fail_over = next(s for s in dev(p, "A1")["steps"] if s["action"] == "fail over")
        self.assertIn("already upgraded passive peer takes over", fail_over["note"])

    def pair_with_the_passive_already_upgraded(self):
        a1, a2 = pair("A1", "A2", ha_config_sync=False)
        a2["sw_version"] = "20.2.9"
        a2["facts"]["sw_version"] = "20.2.9"
        return fleet([a1, a2])

    def test_an_active_whose_peer_is_already_upgraded_is_not_asked_to_sync_its_config_across_versions(self):
        p = plan(self.pair_with_the_passive_already_upgraded(), Selection(serials=["A1"]))
        a1 = dev(p, "A1")
        check = next(r for r in a1["results"] if r["check"] == "ha_peer")
        self.assertEqual(check["status"], "pass")
        self.assertIn("different versions cannot sync", check["reason"])
        self.assertNotIn("force_config_sync", [c["code"] for c in p["cautions"]])
        self.assertNotIn("sync to peer", actions(a1))
        self.assertNotIn("failover_to_unsynced_passive", [c["code"] for c in p["cautions"]])

    def test_the_same_pair_on_the_same_version_still_gets_the_sync(self):
        p = plan(fleet(pair("A1", "A2", ha_config_sync=False)))
        self.assertIn("force_config_sync", [c["code"] for c in p["cautions"]])

    def test_a_standalone_firewall_has_no_peer_to_wait_for(self):
        self.assertNotIn("wait for the passive firewall's upgrade to finish", actions(dev(plan(fleet(device("S1", version="20.1.4")), Selection(accept_standalone_outage=True)), "S1")))

    def test_the_step_listing_names_each_firewall_with_the_role_it_has_now(self):
        from panupgrade import console, report
        p = plan(fleet(pair("A1", "A2")))
        names = [n for group in report.step_groups(p) for n in group[0]]
        self.assertIn("a1 (currently active, after a2)", names)
        self.assertIn("a2 (currently passive)", names)
        text = report.to_text(p)
        self.assertIn("a1 (currently active, after a2):", text)
        self.assertIn("wait for the passive firewall's upgrade to finish", text)
        self.assertIn("a2 (currently passive)", console.render_plan(p, console.Style()))

    def test_actives_of_different_pairs_share_one_listing(self):
        from panupgrade import report
        p = plan(fleet(pair("A1", "A2"), pair("B1", "B2")))
        groups = report.step_groups(p)
        self.assertEqual(len(groups), 2)                                  # all passives, all actives
        self.assertIn(["a1 (currently active, after a2)", "b1 (currently active, after b2)"], [sorted(g[0]) for g in groups])

    def test_preemption_holds_member_until_go_live(self):
        p = plan(fleet(pair("A1", "A2", preemption=True)))
        for s in ("A1", "A2"):
            a = actions(dev(p, s))
            self.assertLess(a.index("hold suspended"), a.index("install 20.2.9"))
            self.assertLess(a.index("install 20.2.9"), a.index("go live"))
            self.assertLess(a.index("go live"), a.index("verify HA"))
        p = plan(fleet(pair("A1", "A2")))
        self.assertNotIn("hold suspended", actions(dev(p, "A1")))

    def test_restore_roles_option_adds_step(self):
        self.assertIn("restore roles", actions(dev(plan(fleet(pair("A1", "A2")), restore_roles="restore"), "A1")))
        self.assertNotIn("restore roles", actions(dev(plan(fleet(pair("A1", "A2"))), "A1")))     # the default leaves the roles alone
        self.assertNotIn("restore roles", actions(dev(plan(fleet(pair("A1", "A2"))), "A1")))

    def test_cross_feature_path_becomes_download_and_install_steps(self):
        p = plan(fleet(pair("A1", "A2", version="20.1.4")))
        self.assertEqual(actions(dev(p, "A2"))[:3], ["download 20.2.0", "download 20.2.9", "backup"])

    def test_every_pair_is_its_own_unit_and_the_order_does_not_depend_on_how_many_run_at_once(self):
        src = fleet(pair("A1", "A2"), pair("B1", "B2"), pair("C1", "C2"))
        self.assertEqual(plan(src)["order"], [["A2", "A1"], ["B2", "B1"], ["C2", "C1"]])         # a flat list of units, each in series
        self.assertEqual(plan(src, max_parallel=2)["order"], plan(src)["order"])                 # no batches: the next unit starts when one finishes

    def test_image_source_is_recorded_in_download_step(self):
        d = dev(plan(fleet(pair("A1", "A2")), image_source="panorama"), "A2")
        self.assertIn("panorama", d["steps"][0]["note"])


class Standalone(unittest.TestCase):
    def test_excluded_unless_accepted(self):
        p = plan(fleet(device("S1")))
        self.assertEqual((dev(p, "S1")["verdict"], dev(p, "S1")["skip_kind"]), ("skipped", "excluded"))
        p = plan(fleet(device("S1")), Selection(accept_standalone_outage=True))
        self.assertEqual(dev(p, "S1")["verdict"], "ready")
        self.assertEqual(p["summary"]["outage_steps"], 1)


class ReachabilityAndData(unittest.TestCase):
    def test_unreachable_and_error_devices_are_skipped_no_data_and_do_not_stop_the_batch(self):
        src = fleet(pair("A1", "A2"), device("D1", connected=False, facts={}),
                    device("E1", collect_error="timed out", facts={}))
        p = plan(src)
        self.assertEqual(dev(p, "D1")["skip_kind"], "no_data")
        self.assertEqual(dev(p, "E1")["skip_kind"], "no_data")
        self.assertEqual(dev(p, "A1")["verdict"], "ready")
        self.assertEqual(p["summary"]["skipped_by_kind"]["no_data"], 2)

    def test_required_check_without_data_blocks(self):
        d = pair("A1", "A2")
        d[1]["facts"]["disk_free_mb"] = None
        self.assertEqual(dev(plan(fleet(d)), "A2")["verdict"], "blocked")

    def test_low_disk_space_blocks_unless_every_needed_image_is_already_on_the_firewall(self):
        """Preparing downloads the image, which uses the very space the check wants free: that must not block the upgrade it prepared for."""
        d = pair("A1", "A2")
        d[0]["facts"]["disk_free_mb"] = d[1]["facts"]["disk_free_mb"] = 3584
        blocked = plan(fleet(d), min_free_disk_mb=4096)
        self.assertEqual(dev(blocked, "A1")["verdict"], "blocked")
        d[0]["facts"]["images_downloaded"] = [TARGET]
        d[1]["facts"]["images_downloaded"] = [TARGET]
        ready = plan(fleet(d), min_free_disk_mb=4096)
        self.assertEqual(dev(ready, "A1")["verdict"], "ready")
        check = next(r for r in dev(ready, "A1")["results"] if r["check"] == "disk_space")
        self.assertIn("already on the firewall", check["reason"])

    def test_optional_check_without_data_does_not_block(self):
        d = pair("A1", "A2")
        d[1]["facts"]["ntp_synced"] = None
        self.assertEqual(dev(plan(fleet(d)), "A2")["verdict"], "ready")

    def test_a_crashing_check_is_an_error_and_blocks(self):
        d = pair("A1", "A2")
        d[1]["facts"]["disk_free_mb"] = "lots"   # makes the comparison raise
        p = plan(fleet(d))
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")
        self.assertTrue(any(r["status"] == "error" for r in dev(p, "A2")["results"]))

    def test_already_at_target_is_not_applicable(self):
        p = plan(fleet(device("S1", version="20.2.9")), Selection(accept_standalone_outage=True))
        self.assertEqual((dev(p, "S1")["verdict"], dev(p, "S1")["skip_kind"]), ("skipped", "not_applicable"))

    def test_no_path_rule_blocks_with_a_reason_not_a_guess(self):
        p = plan(fleet(pair("A1", "A2", version="20.1.4")), rules={"verified": True, "skip": {"from": "20.2"}, "legacy": {"preferred": {}}})
        self.assertEqual(dev(p, "A1")["verdict"], "blocked")           # stepping through 20.1 needs a pinned maintenance release, and there is none
        self.assertIn("not guessing", " ".join(r["reason"] for r in dev(p, "A1")["results"]))


class PendingChanges(unittest.TestCase):
    def pend(self, **over):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True
        return dev(plan(fleet(d), **over), "A2")["verdict"]

    def test_block_by_default_user_can_loosen(self):
        self.assertEqual(self.pend(), "blocked")
        self.assertEqual(self.pend(pending_changes="warn"), "caution")
        self.assertEqual(self.pend(pending_changes="ignore"), "ready")


class BlockedPeer(unittest.TestCase):
    """Half a pair is not upgraded against a problem the tool has just found with the other half."""

    def blocked_passive(self, **over):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True                    # A2 (passive) is blocked
        return plan(fleet(d), **over)

    def test_the_active_member_is_held_when_its_passive_peer_is_blocked_and_says_why(self):
        p = self.blocked_passive()
        self.assertEqual(dev(p, "A2")["verdict"], "blocked")
        a1 = dev(p, "A1")
        self.assertEqual(a1["verdict"], "blocked")
        reason = next(r["reason"] for r in a1["results"] if r["check"] == "peer_blocked")
        self.assertIn("a2", reason)
        self.assertIn("pending_changes", reason)
        self.assertIn("hand the traffic", reason)
        self.assertEqual(p["order"], [])                           # nothing at all is planned for this pair

    def test_the_passive_member_is_held_when_its_active_peer_is_blocked(self):
        d = pair("A1", "A2")
        d[0]["facts"]["pending_changes"] = True                    # A1 (active) is blocked
        p = plan(fleet(d))
        self.assertEqual((dev(p, "A1")["verdict"], dev(p, "A2")["verdict"]), ("blocked", "blocked"))
        self.assertIn("two releases", next(r["reason"] for r in dev(p, "A2")["results"] if r["check"] == "peer_blocked"))

    def test_the_setting_can_let_it_through_as_a_caution(self):
        p = self.blocked_passive(blocked_peer="caution")
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn("active_before_passive", [c["code"] for c in p["cautions"]])
        self.assertEqual(p["order"], [["A1"]])

    def test_a_peer_that_is_only_left_out_of_the_selection_is_still_just_a_caution(self):
        p = plan(fleet(pair("A1", "A2")), Selection(serials=["A1"]))
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn("active_before_passive", [c["code"] for c in p["cautions"]])

    def test_a_peer_that_cannot_be_read_holds_the_other_member_too(self):
        d = pair("A1", "A2")
        d[1]["collect_error"] = "device is slow to answer"
        p = plan(fleet(d))
        self.assertEqual(dev(p, "A2")["verdict"], "skipped")        # no data: it is left alone
        self.assertEqual(dev(p, "A1")["verdict"], "blocked")

    def test_other_pairs_are_not_affected(self):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True
        p = plan(fleet(d, pair("B1", "B2")))
        self.assertEqual(p["order"], [["B2", "B1"]])


class SessionBaseline(unittest.TestCase):
    def test_missing_session_count_is_no_data_not_a_crash(self):
        d = pair("A1", "A2")
        d[0]["facts"]["session_count"] = None
        p = plan(fleet(d))
        r = next(r for r in dev(p, "A1")["results"] if r["check"] == "session_baseline")
        self.assertEqual((r["status"], r["kind"]), ("skip", "no_data"))
        self.assertEqual(dev(p, "A1")["verdict"], "ready")   # not a required check

    def test_zero_sessions_cannot_prove_anything_and_needs_acknowledgement(self):
        d = pair("A1", "A2")
        d[0]["facts"]["session_count"] = 0
        self.assertEqual(dev(plan(fleet(d)), "A1")["verdict"], "caution")

    def test_passive_node_is_never_judged_on_sessions(self):
        r = next(r for r in dev(plan(fleet(pair("A1", "A2"))), "A2")["results"] if r["check"] == "session_baseline")
        self.assertEqual(r["kind"], "not_applicable")


class HaCautions(unittest.TestCase):
    def test_active_without_passive_in_plan_gets_best_practice_caution(self):
        p = plan(fleet(pair("A1", "A2")), Selection(serials=["A1"]))
        self.assertEqual(dev(p, "A1")["verdict"], "caution")
        self.assertIn("active_before_passive", [c["code"] for c in p["cautions"]])
        self.assertEqual(dev(p, "A2")["skip_kind"], "excluded")

    def test_no_caution_when_peer_is_in_plan_or_already_current(self):
        self.assertEqual(plan(fleet(pair("A1", "A2")))["cautions"], [])
        d = pair("A1", "A2")
        d[1]["sw_version"] = "20.2.9"
        d[1]["facts"]["sw_version"] = "20.2.9"
        p = plan(fleet(d), Selection(serials=["A1"]))
        self.assertEqual(dev(p, "A1")["verdict"], "ready")

    def test_blocked_passive_holds_the_active_by_default_and_only_cautions_it_when_asked(self):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True
        self.assertEqual(dev(plan(fleet(d)), "A1")["verdict"], "blocked")
        self.assertEqual(dev(plan(fleet(d), blocked_peer="caution"), "A1")["verdict"], "caution")

    def test_unhealthy_ha_sync_blocks(self):
        d = pair("A1", "A2")
        d[0]["facts"]["ha_session_sync"] = False
        self.assertEqual(dev(plan(fleet(d)), "A1")["verdict"], "blocked")


class LopsidedPairs(unittest.TestCase):
    def codes(self, p, scope):
        return [c["code"] for c in p["cautions"] if c["scope"] == scope]

    def test_selecting_only_the_passive_member_warns_that_the_pair_will_be_lopsided(self):
        p = plan(fleet(pair("A1", "A2")), Selection(serials=["A2"]))
        self.assertIn("lopsided_pair", self.codes(p, "A2"))
        self.assertEqual(dev(p, "A2")["verdict"], "caution")          # a warning, not a block

    def test_selecting_both_members_is_quiet(self):
        p = plan(fleet(pair("A1", "A2")))
        self.assertNotIn("lopsided_pair", self.codes(p, "A2"))
        self.assertNotIn("active_before_passive", self.codes(p, "A1"))

    def test_the_second_half_of_a_pair_run_in_two_stages_is_quiet(self):
        d = pair("A1", "A2")
        d[1]["sw_version"] = TARGET                                    # the passive was done in the first run
        d[1]["facts"]["sw_version"] = TARGET
        p = plan(fleet(d), Selection(serials=["A1"]))
        self.assertEqual(self.codes(p, "A1"), [])

    def test_a_passive_whose_peer_is_not_visible_is_also_flagged(self):
        d = pair("A1", "A2")
        d[1]["peer_serial"] = "GHOST"
        d[1]["facts"]["peer_serial"] = "GHOST"
        self.assertIn("lopsided_pair", self.codes(plan(fleet(d), Selection(serials=["A2"])), "A2"))


class FleetChecks(unittest.TestCase):
    def codes(self, p):
        return [c["code"] for c in p["cautions"]]

    def test_panorama_older_than_target(self):
        self.assertIn("panorama_older_than_target", self.codes(plan(fleet(pair("A1", "A2"), panorama="20.1.0"))))
        self.assertNotIn("panorama_older_than_target", self.codes(plan(fleet(pair("A1", "A2")))))

    def advice(self, p):
        return [(a["scope"], a["code"]) for a in p["advice"]]

    def test_lagging_firewalls_get_advice_not_a_blocker_or_a_caution(self):
        p = plan(fleet(pair("A1", "A2"), panorama="23.0.0"), panorama_compare="major")   # 23 vs 20 = 3 > 2
        self.assertIn(("fleet", "version_lagging"), self.advice(p))
        self.assertNotIn("version_spread", self.codes(p))
        self.assertEqual(dev(p, "A1")["verdict"], "ready")      # the lag does not hold the upgrade

    def test_every_lagging_firewall_is_named_once_in_one_note_and_the_advice_is_not_repeated(self):
        p = plan(fleet(pair("A1", "A2"), device("OLD", version="20.1.4"), panorama="23.0.0"), panorama_compare="major")
        notes = [a for a in p["advice"] if a["code"] == "version_lagging"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "PAN-OS version lagging behind Panorama")
        self.assertEqual(sorted(d["serial"] for d in notes[0]["devices"]), ["A1", "A2", "OLD"])
        self.assertEqual(set(notes[0]["devices"][0]), {"serial", "hostname", "version"})
        self.assertEqual(sum(1 for a in p["advice"] if "Panorama converts" in a["text"]), 1)
        self.assertEqual([a for a in p["advice"] if a["scope"] in ("A1", "A2", "OLD")], [])      # nothing repeated per firewall

    def test_the_text_and_console_plans_list_them_in_columns_under_one_heading_and_not_again_per_firewall(self):
        from panupgrade import console, report
        p = plan(fleet(pair("A1", "A2"), panorama="23.0.0"), panorama_compare="major")
        text = report.to_text(p)
        self.assertEqual(text.count("PAN-OS version lagging behind Panorama"), 1)
        self.assertEqual(text.count("Panorama converts"), 1)
        self.assertRegex(text, r"\n      A1  \S+  20\.")
        self.assertNotIn("panorama_range", text)
        shown = console.render_plan(p, console.Style())
        self.assertEqual(shown.count("PAN-OS version lagging behind Panorama"), 1)
        self.assertEqual(shown.count("Panorama converts"), 1)
        self.assertNotIn("panorama_range:", shown)
        self.assertIn("panorama_range", report.to_text(p, verbose=True))                            # the detail is still there on request

    def test_spread_limit_is_tunable(self):
        p = plan(fleet(pair("A1", "A2"), panorama="23.0.0"), spread_limit=3, panorama_compare="major")
        self.assertEqual(self.advice(p), [])

    def test_advice_covers_firewalls_left_behind_by_the_selection(self):
        src = fleet(pair("A1", "A2"), device("OLD", version="20.1.4"), panorama="23.0.0")
        p = plan(src, Selection(serials=["A1", "A2"]), panorama_compare="major")
        self.assertIn(("fleet", "version_lagging"), self.advice(p))

    def test_advice_says_when_the_target_is_itself_out_of_range(self):
        p = plan(fleet(pair("A1", "A2"), panorama="23.0.0"), panorama_compare="major")     # target 20.2.9 is itself 3 behind 23
        text = " ".join(a["text"] for a in p["advice"] if a["code"] == "version_lagging")
        self.assertEqual(text.count("consider a newer target"), 1)

    def test_panorama_older_is_judged_by_minor_version_by_default_and_by_major_if_told(self):
        # Panorama 20.2.5 vs target 20.2.9: same feature release, so not older
        self.assertNotIn("panorama_older_than_target", self.codes(plan(fleet(pair("A1", "A2"), panorama="20.2.5"))))
        # Panorama 20.1.0 vs target 20.2.9: an older feature release, but the same major
        self.assertIn("panorama_older_than_target", self.codes(plan(fleet(pair("A1", "A2"), panorama="20.1.0"))))
        self.assertNotIn("panorama_older_than_target",
                         self.codes(plan(fleet(pair("A1", "A2"), panorama="20.1.0"), panorama_compare="major")))

    def test_minor_distance_is_counted_along_the_releases_that_exist_not_along_the_numbers(self):
        from panupgrade import releases
        from panupgrade.versions import Version
        table = releases.Releases([{"release": r} for r in ("10.2", "11.0", "11.1", "11.2", "12.1", "12.2")])
        behind = lambda pano, fw, unit="minor": table.behind(Version.parse(pano), Version.parse(fw), unit)
        self.assertEqual(behind("12.2.0", "11.2.0"), 2)          # 12.2 > 12.1 > 11.2: there is no 12.0
        self.assertEqual(behind("12.1.10", "10.2.2"), 4)
        self.assertEqual(behind("12.1.10", "10.2.2", "major"), 2)
        self.assertEqual(behind("11.2.0", "11.2.5"), 0)          # level, or the firewall is ahead
        self.assertEqual(behind("11.1.0", "11.2.0"), 0)
        self.assertEqual(behind("13.1.0", "11.2.0"), 3)          # 13.1 is not in the table: placed by its numbers, 11.2 > 12.1 > 12.2 > 13.1

    def test_a_release_the_table_lacks_is_said_so_in_the_advice(self):
        p = plan(fleet(pair("A1", "A2"), panorama="23.0.0"), panorama_compare="minor")
        text = " ".join(a["text"] for a in p["advice"]) + " ".join(r["reason"] for r in dev(p, "A1")["results"] if r["check"] == "panorama_range")
        self.assertIn("release table does not list", text)


class PlanIdentity(unittest.TestCase):
    def test_approval_hash_ignores_time_and_drifting_numbers_but_not_decisions(self):
        from datetime import datetime, timezone
        a = build_plan(fleet(pair("A1", "A2")), TARGET, settings.load(), rules=TEST_RULES,
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        b = build_plan(fleet(pair("A1", "A2")), TARGET, settings.load(), rules=TEST_RULES,
                       now=datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(a["hash"], b["hash"])
        # numbers that drift minute to minute must not make an approval go stale ...
        drift = pair("A1", "A2")
        drift[0]["facts"]["session_count"] = 999
        drift[0]["facts"]["disk_free_mb"] = 8123
        drifted = plan(fleet(drift))
        self.assertEqual(a["hash"], drifted["hash"])
        self.assertNotEqual(a["content_hash"], drifted["content_hash"])        # the full content still differs
        # ... but anything a person decides on must
        swapped = pair("A1", "A2")
        swapped[0]["facts"]["ha_state"], swapped[1]["facts"]["ha_state"] = "passive", "active"
        swapped[0]["ha_state"], swapped[1]["ha_state"] = "passive", "active"
        self.assertNotEqual(a["hash"], plan(fleet(swapped))["hash"])             # roles swapped: different steps
        pending = pair("A1", "A2")
        pending[1]["facts"]["pending_changes"] = True
        self.assertNotEqual(a["hash"], plan(fleet(pending))["hash"])             # a new caution appeared
        self.assertNotEqual(a["hash"], plan(fleet(pair("A1", "A2", version="20.2.4")))["hash"])

    def test_bad_target_is_a_plan_error(self):
        with self.assertRaises(PlanError):
            build_plan(fleet(), "twenty", settings.load())


class Selections(unittest.TestCase):
    def test_filters(self):
        src = fleet(device("S1", model="SIM-A", device_group="dg1", tags=["lab"]),
                    device("S2", model="SIM-B", device_group="dg2", tags=["prod"]))

        def sel(**k):
            return Selection(accept_standalone_outage=True, **k)

        def planned(s):
            return {d["serial"] for d in plan(src, s)["devices"] if d["verdict"] != "skipped"}
        self.assertEqual(planned(sel(models=["SIM-B"])), {"S2"})
        self.assertEqual(planned(sel(device_groups=["dg1"])), {"S1"})
        self.assertEqual(planned(sel(tags=["prod"])), {"S2"})
        self.assertEqual(planned(sel(name_glob="s*")), {"S1", "S2"})
        self.assertEqual(planned(sel(serials=["S2"])), {"S2"})


class NoStaleState(unittest.TestCase):
    def test_each_device_reports_only_its_own_facts(self):
        # the old script could show the previous device's ending version for a device whose backup failed
        src = fleet(device("S1", version="20.2.1"), device("S2", version="20.2.2", collect_error="boom", facts={}),
                    device("S3", version="20.2.3"))
        p = plan(src, Selection(accept_standalone_outage=True))
        self.assertEqual([dev(p, s)["current_version"] for s in ("S1", "S2", "S3")], ["20.2.1", "20.2.2", "20.2.3"])
        self.assertEqual(dev(p, "S2")["steps"], [])
        self.assertEqual(dev(p, "S2")["verdict"], "skipped")


if __name__ == "__main__":
    unittest.main()


class ASuspendedMember(unittest.TestCase):
    """A member that is suspended when planned carries no traffic: upgraded like a passive member unless upgrade_suspended is false."""

    @staticmethod
    def suspended_pair():
        data = pair("A1", "A2")
        data[1]["facts"]["ha_state"] = "suspended"
        return data

    def test_it_is_planned_like_a_passive_member_by_default(self):
        p = plan(fleet(self.suspended_pair()))
        d = dev(p, "A2")
        self.assertEqual(d["verdict"], "ready")
        self.assertNotIn("fail over", actions(d))
        self.assertNotIn("go live", actions(d))
        self.assertIn("verify HA", actions(d))
        self.assertEqual([r["status"] for r in d["results"] if r["check"] == "session_baseline"], ["skip"])

    def test_the_suspended_member_goes_first_in_its_pair(self):
        p = plan(fleet(self.suspended_pair()))
        self.assertEqual(p["order"], [["A2", "A1"]])
        self.assertIn("wait for the passive firewall's upgrade to finish", actions(dev(p, "A1")))

    def test_upgrade_suspended_false_blocks_it_and_says_which_setting(self):
        p = plan(fleet(self.suspended_pair()), upgrade_suspended=False)
        d = dev(p, "A2")
        self.assertEqual(d["verdict"], "blocked")
        self.assertTrue(any("upgrade_suspended" in r["reason"] for r in d["results"] if r["check"] == "ha_state"))

    def test_the_setting_is_part_of_the_plan_hash(self):
        a, b = plan(fleet(self.suspended_pair())), plan(fleet(self.suspended_pair()), upgrade_suspended=False)
        self.assertNotEqual(a["hash"], b["hash"])
