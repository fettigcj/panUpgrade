"""A refused plan says what changed; a journal that already exists leads somewhere instead of dumping the person at the prompt."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from panupgrade import approval, cli, console, settings
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet
from tests.support import TARGET, TEST_RULES, fleet_data, pair

STYLE = console.Style(False, False)


def snap(**over):
    base = {"target": "10.2.2", "panorama_version": "12.1", "settings": {"max_parallel": 1},
            "devices": [{"serial": "S1", "version": "10.2.1", "ha_mode": "active-passive", "ha_state": "active", "peer": "S2",
                         "verdict": "caution", "acks": ["failover_to_unsynced_passive"], "steps": [["download", False], ["install", False]]},
                        {"serial": "S2", "version": "10.2.1", "ha_mode": "active-passive", "ha_state": "passive", "peer": "S1",
                         "verdict": "ready", "acks": [], "steps": [["download", False]]}],
            "order": [[["S2", "S1"]]], "fleet_acks": [], "hostnames": {"S1": "fw-one", "S2": "fw-two"}}
    base.update(over)
    return base


class WhatChanged(unittest.TestCase):
    def test_nothing_to_say_when_nothing_differs_even_after_a_json_round_trip(self):
        self.assertEqual(approval.what_changed(json.loads(json.dumps(snap())), snap()), [])

    def test_names_the_firewall_and_the_difference(self):
        new = snap()
        new["devices"][0].update(ha_state="passive", version="10.2.2")
        new["devices"][1]["ha_state"] = "active"
        new["devices"][0]["acks"] = ["failover_to_unsynced_passive", "no_session_baseline"]
        text = "\n".join(approval.what_changed(snap(), new))
        self.assertIn("fw-one (S1): HA role active -> passive", text)
        self.assertIn("fw-one (S1): running version 10.2.1 -> 10.2.2", text)
        self.assertIn("fw-two (S2): HA role passive -> active", text)
        self.assertIn("NEW caution [no_session_baseline]", text)

    def test_devices_added_removed_and_settings(self):
        new = snap(settings={"max_parallel": 3}, target="10.2.3", devices=snap()["devices"][:1])
        text = "\n".join(approval.what_changed(snap(), new))
        self.assertIn("target version: 10.2.2 -> 10.2.3", text)
        self.assertIn("setting max_parallel: 1 -> 3", text)
        self.assertIn("fw-two (S2): no longer in the plan", text)

    def test_an_approval_file_without_a_snapshot_still_refuses_and_says_so(self):
        plan = {"hash": "new", "snapshot": snap(), "devices": [], "cautions": []}
        problems = approval.Approval("old", ["S1"]).problems(plan)
        self.assertTrue(any("did not record enough" in p for p in problems))

    def test_the_refusal_lists_the_differences(self):
        new = snap()
        new["devices"][0]["ha_state"] = "passive"
        plan = {"hash": "new", "snapshot": new, "devices": [{"serial": "S1", "verdict": "caution"}], "cautions": []}
        problems = approval.Approval("old", ["S1"], snapshot=snap()).problems(plan)
        self.assertIn("What is different now", problems[0])
        self.assertTrue(any("HA role active -> passive" in p for p in problems))


class TheApprovalCoversOnlyWhatItApproves(unittest.TestCase):
    """Seen live: a kit prepared with the whole fleet read refused itself when run on just its own firewalls, because the other
    firewalls (not selected, so not even read the second time) changed shape in the plan hash."""

    def plan(self, selection):
        data = fleet_data(pair("A1", "A2", ha_config_sync=True), pair("B1", "B2", version=TARGET))
        s = settings.load(None, [])
        return build_plan(FakeFleet(data), TARGET, s, selection, TEST_RULES)

    def test_the_hash_is_the_same_whether_or_not_the_other_firewalls_were_in_scope(self):
        whole = self.plan(Selection())
        narrow = self.plan(Selection(serials=["A1", "A2"]))
        self.assertEqual({d["serial"] for d in whole["devices"] if d["verdict"] in ("ready", "caution")}, {"A1", "A2"})
        self.assertEqual(whole["hash"], narrow["hash"])

    def test_a_change_to_an_approved_firewall_still_changes_the_hash(self):
        data = fleet_data(pair("A1", "A2", ha_config_sync=True))
        for d in data["devices"]:
            d["facts"]["ha_state"] = "passive" if d["facts"]["ha_state"] == "active" else "active"
            d["ha_state"] = d["facts"]["ha_state"]
        s = settings.load(None, [])
        swapped = build_plan(FakeFleet(data), TARGET, s, Selection(), TEST_RULES)
        self.assertNotEqual(self.plan(Selection())["hash"], swapped["hash"])


class ExistingJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "run.journal.jsonl")
        self.args = argparse.Namespace(journal=str(self.path), resume=False, fresh=False, dry_run=False, runs_dir=self.tmp.name)

    def choose(self, answer=None, tty=True):
        return cli._choose_journal(self.args, {"hash": "h"}, "upgrade", STYLE, input_fn=lambda _: answer, tty=tty)

    def test_a_journal_that_only_has_its_header_is_simply_reused(self):
        Journal.create(str(self.path), "h")                      # what a refused run used to leave behind
        self.assertEqual(self.choose(tty=False), (str(self.path), True))

    def started_run(self):
        journal = Journal.create(str(self.path), "h")
        journal.record("S1", "download", "ok")

    def test_a_started_run_is_offered_back_at_a_terminal(self):
        self.started_run()
        self.assertEqual(self.choose("continue"), (str(self.path), True))

    def test_new_names_a_fresh_file_beside_it(self):
        self.started_run()
        path, resume = self.choose("new")
        self.assertFalse(resume)
        self.assertNotEqual(path, str(self.path))
        self.assertTrue(path.endswith("-2.jsonl"))

    def test_anything_else_cancels_and_without_a_terminal_it_says_what_to_add(self):
        self.started_run()
        with self.assertRaises(ValueError):
            self.choose("x")
        with self.assertRaisesRegex(ValueError, "Add --resume"):
            self.choose(tty=False)


if __name__ == "__main__":
    unittest.main()
