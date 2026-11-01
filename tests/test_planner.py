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


class HappyPaths(unittest.TestCase):
    def test_pair_is_one_unit_passive_first(self):
        p = plan(fleet(pair("A1", "A2")))
        self.assertEqual(p["order"], [[["A2", "A1"]]])
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

    def test_a_standalone_firewall_has_no_peer_to_wait_for(self):
        self.assertNotIn("wait for the passive firewall's upgrade to finish", actions(dev(plan(fleet(device("S1", version="20.1.4")), Selection(accept_standalone_outage=True)), "S1")))

    def test_the_step_listing_names_each_firewall_with_the_role_it_has_now(self):
        from panupgrade import console, report
        p = plan(fleet(pair("A1", "A2")))
        names = [n for group in report.step_groups(p) for n in group[0]]
        self.assertIn("a1 (currently active)", names)
        self.assertIn("a2 (currently passive)", names)
        text = report.to_text(p)
        self.assertIn("a1 (currently active):", text)
        self.assertIn("wait for the passive firewall's upgrade to finish", text)
        self.assertIn("a2 (currently passive)", console.render_plan(p, console.Style()))

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

    def test_max_parallel_makes_batches(self):
        src = fleet(pair("A1", "A2"), pair("B1", "B2"), pair("C1", "C2"))
        self.assertEqual(len(plan(src)["order"]), 3)
        self.assertEqual([len(b) for b in plan(src, max_parallel=2)["order"]], [2, 1])

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
        p = plan(fleet(pair("A1", "A2", version="20.1.4")), rules={"verified": False, "direct_from": {}})
        self.assertEqual(dev(p, "A1")["verdict"], "blocked")


class PendingChanges(unittest.TestCase):
    def pend(self, **over):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True
        return dev(plan(fleet(d), **over), "A2")["verdict"]

    def test_block_by_default_user_can_loosen(self):
        self.assertEqual(self.pend(), "blocked")
        self.assertEqual(self.pend(pending_changes="warn"), "caution")
        self.assertEqual(self.pend(pending_changes="ignore"), "ready")


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

    def test_blocked_passive_makes_active_a_caution(self):
        d = pair("A1", "A2")
        d[1]["facts"]["pending_changes"] = True
        self.assertEqual(dev(plan(fleet(d)), "A1")["verdict"], "caution")

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
