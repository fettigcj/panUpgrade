# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

import unittest

import tests.support  # noqa: F401  (puts src on the path)
from panupgrade import runlog

HOSTS = {"S1": "fw-bravo", "S2": "fw-alpha"}
PLAN = {"target": "10.2.10-h42", "hash": "0123456789abcdef0123"}


def step(ts, serial, name, state, **detail):
    return {"ts": ts, "type": "step", "serial": serial, "step": name, "state": state, "detail": detail}


EVENTS = [step("2026-11-03T10:00:00Z", "S1", "download 10.2.10-h42", "started"),
          step("2026-11-03T10:00:01Z", "S2", "download 10.2.10-h42", "started"),
          step("2026-11-03T10:00:09Z", "S2", "download 10.2.10-h42", "ok", version="10.2.10-h42"),
          step("2026-11-03T10:01:30Z", "S1", "download 10.2.10-h42", "failed", error="<read timed out>")]
RESULT = {"outcome": {"S1": ("failed", "download: <read timed out>"), "S2": ("done", "")}, "failures": 1, "stopped": False}


class RunLog(unittest.TestCase):
    def build(self, result=RESULT, plan=PLAN, events=EVENTS):
        return runlog.build("preparation", plan, HOSTS, events, "LIVE", "/x/prepare.jsonl", result)

    def test_files_are_named_for_what_they_hold(self):
        self.assertEqual(runlog.file_name("PreparationReport", "10.2.10-h42", "20261103-100000", "html"),
                         "PanUpgrade_PreparationReport_10.2.10-h42_20261103-100000.html")

    def test_there_is_an_all_activity_tab_and_one_tab_per_hostname_in_name_order(self):
        page = runlog.to_html(self.build())
        self.assertIn('data-panel="all">All activity', page)
        self.assertEqual([t["host"] for t in self.build()["tabs"]], ["fw-alpha", "fw-bravo"])
        self.assertIn('data-panel="fw1">fw-alpha', page)
        self.assertIn('data-panel="fw2">fw-bravo', page)

    def test_all_activity_has_every_event_with_its_hostname_in_the_order_written(self):
        everything = self.build()["all"]
        self.assertEqual([(host, kind) for _, host, kind, _ in everything],
                         [("fw-bravo", "start"), ("fw-alpha", "start"), ("fw-alpha", "ok"), ("fw-bravo", "fail")])

    def test_each_firewall_tab_holds_only_its_own_events(self):
        tabs = {t["host"]: t for t in self.build()["tabs"]}
        self.assertEqual(len(tabs["fw-alpha"]["rows"]), 2)
        self.assertEqual(len(tabs["fw-bravo"]["rows"]), 2)
        self.assertEqual((tabs["fw-alpha"]["state"], tabs["fw-bravo"]["state"]), ("done", "failed"))

    def test_text_in_events_is_escaped_in_the_page(self):
        page = runlog.to_html(self.build())
        self.assertIn("&lt;read timed out&gt;", page)
        self.assertNotIn("<read timed out>", page)

    def test_the_verdict_counts_the_outcome(self):
        data = self.build()
        self.assertEqual(data["verdict"], "1 of 2 firewalls finished all their steps, 1 failed.")
        self.assertTrue(data["failed"])

    def test_a_run_that_did_not_finish_says_so_and_marks_unfinished_steps_in_doubt(self):
        data = self.build(result=None, events=EVENTS[:2])
        self.assertIn("did not finish", data["verdict"])
        self.assertEqual({t["host"]: t["state"] for t in data["tabs"]}, {"fw-alpha": "in doubt", "fw-bravo": "in doubt"})

    def test_a_preparation_without_a_target_still_gets_a_name(self):
        self.assertEqual(self.build(plan={"target": None, "hash": "ab" * 10})["target"], runlog.NO_TARGET)

    def test_an_event_for_an_unknown_serial_is_shown_under_the_serial(self):
        data = self.build(events=[step("2026-11-03T10:00:00Z", "ZZ9", "preflight", "started")])
        self.assertEqual(data["tabs"][0]["host"], "ZZ9")

    def test_the_text_copy_has_the_same_sections(self):
        text = runlog.to_text(self.build())
        for heading in ("== All activity ==", "== fw-alpha (S2): done ==", "== fw-bravo (S1): failed =="):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
