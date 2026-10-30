"""Pairs in parallel, members in series; one ordered event stream; a polite shared API rate; Ctrl-C semantics."""

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, fleet_data, pair
from panupgrade import console, executor, prepare, settings
from panupgrade.approval import Approval
from panupgrade.journal import Journal
from panupgrade.planner import Selection, build_plan
from panupgrade.simulator import SimBackend
from panupgrade.sources import FakeFleet
from panupgrade_live.panorama import RateLimiter


class Probe(SimBackend):
    """A simulator that is slow in the places a real firewall is, and watches who is busy at the same time."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.mutex, self.busy, self.peak, self.peak_per_pair = threading.Lock(), {}, 0, 0

    def _pair_of(self, serial):
        n = self.nodes[serial]
        return frozenset({serial, n.peer}) if n.peer else frozenset({serial})

    def reboot_and_wait(self, serial, timeout_s):
        key = self._pair_of(serial)
        with self.mutex:
            self.busy[key] = self.busy.get(key, 0) + 1
            self.peak_per_pair = max(self.peak_per_pair, self.busy[key])
            self.peak = max(self.peak, sum(self.busy.values()))
        time.sleep(0.05)
        try:
            return super().reboot_and_wait(serial, timeout_s)
        finally:
            with self.mutex:
                self.busy[key] -= 1


def four_pairs():
    return fleet_data(pair("A1", "A2"), pair("B1", "B2"), pair("C1", "C2"), pair("D1", "D2"))


def run_plan(data, backend, jpath, **opts):
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    plan = build_plan(FakeFleet(data), TARGET, s, Selection(), rules=TEST_RULES)
    ok = [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")]
    approval = Approval(plan["hash"], ok, [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])
    journal = Journal.create(jpath, plan["hash"])
    return plan, journal, executor.run(plan, approval, backend, journal, s)


class PairsInParallelMembersInSeries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.j = str(Path(self._tmp.name, "j.jsonl"))

    def test_pairs_overlap_up_to_max_parallel_but_a_pairs_members_never_do(self):
        probe = Probe(four_pairs())
        _, _, result = run_plan(four_pairs(), probe, self.j, max_parallel=3)
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        self.assertGreaterEqual(probe.peak, 2)                  # real overlap
        self.assertLessEqual(probe.peak, 3)                     # never more than asked for
        self.assertEqual(probe.peak_per_pair, 1)                # passive, then active: never both at once

    def test_the_default_is_one_at_a_time(self):
        probe = Probe(four_pairs())
        run_plan(four_pairs(), probe, self.j)
        self.assertEqual(probe.peak, 1)

    def test_a_parallel_run_ends_where_a_sequential_one_does(self):
        seq = SimBackend(four_pairs())
        _, _, a = run_plan(four_pairs(), seq, self.j)
        par = Probe(four_pairs())
        _, _, b = run_plan(four_pairs(), par, str(Path(self._tmp.name, "j2.jsonl")), max_parallel=4)
        self.assertEqual({k: v[0] for k, v in a["outcome"].items()}, {k: v[0] for k, v in b["outcome"].items()})
        self.assertEqual({s: (n.version, n.state) for s, n in seq.nodes.items()}, {s: (n.version, n.state) for s, n in par.nodes.items()})

    def test_the_journal_stays_whole_and_ordered_under_concurrency(self):
        _, journal, _ = run_plan(four_pairs(), Probe(four_pairs()), self.j, max_parallel=4)
        lines = Path(self.j).read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]                       # every line is complete JSON
        self.assertEqual(len(records) - 1, len(journal.events))              # the header plus exactly the events held in memory
        for serial in {r["serial"] for r in records if r.get("serial")}:     # per firewall: each step started before it finished
            seen = {}
            for r in (r for r in records if r.get("serial") == serial and r["step"] != "baseline"):          # "baseline" is a single note
                if r["state"] == "started":
                    seen[r["step"]] = "started"
                else:
                    self.assertEqual(seen.get(r["step"]), "started", (serial, r["step"]))

    def test_a_failure_in_one_pair_holds_only_its_own_partner(self):
        probe = Probe(four_pairs(), fail={("B2", "install"): "boom"})        # B2 is the passive that goes first
        _, _, result = run_plan(four_pairs(), probe, self.j, max_parallel=4)
        states = {k: v[0] for k, v in result["outcome"].items()}
        self.assertEqual((states["B2"], states["B1"]), ("failed", "held"))
        self.assertEqual([states[s] for s in ("A1", "A2", "C1", "C2", "D1", "D2")], ["done"] * 6)

    def test_the_failure_limit_is_shared_across_threads(self):
        fail = {(s, "install"): "boom" for s in ("A2", "B2", "C2", "D2")}
        _, _, result = run_plan(four_pairs(), Probe(four_pairs(), fail=fail), self.j, max_parallel=1, max_failures=2)
        self.assertEqual(result["failures"], 2)
        self.assertTrue(result["stopped"])
        self.assertEqual([v[0] for k, v in result["outcome"].items() if k in ("C1", "C2", "D1", "D2")], ["not_started"] * 4)

    def test_ctrl_c_flag_stops_new_work_without_undoing_anything(self):
        stop = threading.Event()
        stop.set()
        s = settings.load()
        data = four_pairs()
        plan = build_plan(FakeFleet(data), TARGET, s, Selection(), rules=TEST_RULES)
        approval = Approval(plan["hash"], [d["serial"] for d in plan["devices"]], [f"{c['scope']}:{c['code']}" for c in plan["cautions"]])
        sim = SimBackend(data)
        result = executor.run(plan, approval, sim, Journal.create(self.j, plan["hash"]), s, stop=stop)
        self.assertTrue(all(v[0] == "not_started" for v in result["outcome"].values()))
        self.assertEqual(sim.calls, [])


class PrepareInParallel(unittest.TestCase):
    def test_pairs_prepare_side_by_side_and_each_pair_in_order(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = fleet_data(*[pair(f"{c}1", f"{c}2", ha_config_sync=False) for c in "ABC"])
        s = settings.load(None, ["sync_to_peer=prepare", "max_parallel=3"])
        plan = prepare.build(FakeFleet(data), s, Selection(), TARGET, TEST_RULES)
        sim = SimBackend(data)
        result = prepare.run(plan, Approval(plan["hash"], plan["serials"]), sim, Journal.create(str(Path(tmp.name, "p.jsonl")), plan["hash"]), s)
        self.assertTrue(all(v[0] == "done" for v in result["outcome"].values()), result["outcome"])
        for c in "ABC":                                                      # per pair: backup, then sync, then downloads
            order = [x[1] for x in sim.calls if x[0] in (f"{c}1", f"{c}2") and x[1] in ("backup", "sync to peer", "download")]
            self.assertEqual(order[:2], ["backup", "sync to peer"])
            self.assertEqual(order[2:], ["download", "download"])


class PoliteRate(unittest.TestCase):
    def test_threads_sharing_a_limiter_never_exceed_the_rate(self):
        now = {"t": 0.0}
        slept = []

        def sleep(seconds):                       # virtual time: a sleeping thread just moves the clock forward
            slept.append(seconds)

        limiter = RateLimiter(5.0, clock=lambda: now["t"], sleep=sleep)
        stamps = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                limiter.wait()
                with lock:
                    stamps.append(limiter._next)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        slots = sorted(stamps)
        self.assertEqual(len(slots), 20)
        gaps = [round(b - a, 6) for a, b in zip(slots, slots[1:])]
        self.assertTrue(all(g >= 0.2 - 1e-6 for g in gaps), gaps)           # slots are at least 1/5 s apart
        self.assertGreaterEqual(slots[-1], 3.8 - 1e-6)                      # 20 requests take about 4 s at 5 per second

    def test_the_setting_must_be_positive(self):
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["api_rate_per_s=0"])


class StatusBoardView(unittest.TestCase):
    def test_it_shows_one_line_per_firewall_and_scrolls_failures_above(self):
        out = io.StringIO()
        board = console.StatusBoard(console.Style(True, False), {"A": "fw-a", "B": "fw-b"}, ["A", "B"], out=out, refresh_s=0)
        board({"type": "step", "serial": "A", "step": "0:download 1.2.3", "state": "started", "detail": {}})
        board({"type": "step", "serial": "B", "step": "3:install 1.2.3", "state": "failed", "detail": {"error": "no space"}})
        board.close()
        text = out.getvalue()
        self.assertIn("fw-a", text)
        self.assertIn("download 1.2.3", text)
        self.assertIn("no space", text)                                       # the failure is a permanent log line
        self.assertIn("\033[2A", text)                                        # later redraws move up over the previous board


if __name__ == "__main__":
    unittest.main()
