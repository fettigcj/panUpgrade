"""A real run is carried out by a background worker, and a viewer looks at it: locks, heartbeat, stop, pause, hand-over, dead workers."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from tests.support import TARGET, fleet_data, pair
from panupgrade import approval as approval_mod, cli, executor, settings, worker
from panupgrade_live import launch


class Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.journal = self.dir / "upgrade-1.jsonl"


class TheLock(Folder):
    def test_a_second_worker_is_refused_while_the_first_is_alive(self):
        worker.acquire(self.dir, {"journal": "j1"})
        with self.assertRaisesRegex(worker.WorkerBusy, "already in progress.*j1"):
            worker.acquire(self.dir, {"journal": "j2"})
        self.assertEqual(worker.active(self.dir)["journal"], "j1")

    def test_a_lock_whose_process_has_gone_is_cleared_and_reported(self):
        worker.write_json(worker.lock_file(self.dir), {"pid": 2 ** 22 + 12345, "journal": "old", "started": "then"})
        self.assertIsNone(worker.active(self.dir))
        self.assertEqual(worker.left_behind(self.dir)["journal"], "old")
        worker.acquire(self.dir, {"journal": "new"})                                  # not refused
        self.assertEqual(worker.active(self.dir)["journal"], "new")

    def test_only_the_owner_releases_it(self):
        worker.write_json(worker.lock_file(self.dir), {"pid": os.getpid() + 1, "journal": "x"})
        worker.release(self.dir)
        self.assertTrue(worker.lock_file(self.dir).exists())
        worker.write_json(worker.lock_file(self.dir), {"pid": os.getpid(), "journal": "x"})
        worker.release(self.dir)
        self.assertFalse(worker.lock_file(self.dir).exists())

    def test_this_process_is_alive_and_a_finished_one_is_not(self):
        self.assertTrue(worker.pid_alive(os.getpid()))
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(worker.pid_alive(proc.pid))
        self.assertFalse(worker.pid_alive(None))
        self.assertFalse(worker.pid_alive(0))


class TheHeartbeat(Folder):
    def beat(self, mode="continue", after_s=900, clock=None):
        stop, said = threading.Event(), []
        beat = worker.Heartbeat(self.journal, stop, mode, after_s, say=said.append, clock=clock or time.time)
        return beat, stop, said

    def test_it_writes_the_status(self):
        beat, _, _ = self.beat()
        beat.tick()
        status = worker.status(self.journal)
        self.assertEqual((status["state"], status["pid"], status["exit"]), ("running", os.getpid(), None))

    def test_a_stop_request_from_a_viewer_stops_new_work(self):
        beat, stop, said = self.beat()
        worker.request_stop(self.journal)
        beat.tick()
        self.assertTrue(stop.is_set())
        self.assertEqual(worker.status(self.journal)["state"], "stopping")
        self.assertTrue([m for m in said if "stop requested" in m])

    def test_continue_never_pauses_however_long_nobody_looks(self):
        now = {"t": 1000.0}
        beat, stop, _ = self.beat("continue", 60, clock=lambda: now["t"])
        now["t"] += 10 ** 6
        beat.tick()
        self.assertFalse(stop.is_set())

    def test_pause_stops_new_work_when_nobody_has_looked_for_too_long(self):
        now = {"t": 1000.0}
        beat, stop, said = self.beat("pause", 600, clock=lambda: now["t"])
        now["t"] += 599
        beat.tick()
        self.assertFalse(stop.is_set())
        now["t"] += 2
        beat.tick()
        self.assertTrue(stop.is_set())
        self.assertEqual(worker.status(self.journal)["state"], "pausing")
        self.assertTrue([m for m in said if "nobody has looked" in m and "--resume" in m])
        beat.finish(0)
        self.assertEqual(worker.status(self.journal)["state"], "paused")

    def test_a_viewer_checking_in_keeps_a_pause_setting_from_pausing(self):
        now = {"t": 1000.0}
        beat, stop, _ = self.beat("pause", 600, clock=lambda: now["t"])
        for _ in range(5):
            now["t"] += 500
            worker.checkin(self.journal, clock=lambda: now["t"])
            beat.tick()
        self.assertFalse(stop.is_set())

    def test_the_last_status_says_how_it_ended(self):
        beat, _, _ = self.beat()
        beat.finish(1)
        status = worker.status(self.journal)
        self.assertEqual((status["state"], status["exit"]), ("finished", 1))


class Following(Folder):
    class Proc:
        def __init__(self):
            self.ended, self.returncode = False, None

        def poll(self):
            return self.returncode if self.ended else None

    def test_it_passes_on_output_and_records_and_returns_the_exit_code(self):
        proc, lines, records = self.Proc(), [], []
        log = worker.sidecar(self.journal, ".out")
        log.write_text("one\ntwo\npartial", encoding="utf-8")
        self.journal.write_text(json.dumps({"type": "header", "plan_hash": "h"}) + "\n" +
                                json.dumps({"type": "step", "serial": "A1", "step": "backup", "state": "ok"}) + "\n", encoding="utf-8")
        ticks = {"n": 0}

        def sleep(_):
            ticks["n"] += 1
            if ticks["n"] == 1:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(" line\nlast\n")
            if ticks["n"] == 2:
                worker.write_json(worker.sidecar(self.journal, ".status.json"), {"state": "finished", "exit": 3})
                proc.ended, proc.returncode = True, 3
        code = worker.follow(self.journal, proc=proc, on_line=lines.append, on_record=records.append, sleep=sleep)
        self.assertEqual(code, 3)
        self.assertEqual(lines, ["one", "two", "partial line", "last"])
        self.assertEqual([r.get("serial") for r in records], [None, "A1"])
        self.assertTrue(worker.sidecar(self.journal, ".viewer").exists())              # it said it was looking

    def test_a_viewer_that_finds_the_clock_far_ahead_says_the_computer_was_asleep(self):
        proc, lines, now = self.Proc(), [], {"t": 1000.0}

        def clock():
            return now["t"]

        def sleep(_):
            now["t"] += 1.0
            if now["t"] == 1001.0:
                now["t"] += 7200                       # the lid closed here
            else:
                proc.ended, proc.returncode = True, 0
        self.journal.write_text(json.dumps({"type": "header", "plan_hash": "h"}) + chr(10), encoding="utf-8")
        worker.follow(self.journal, proc=proc, on_line=lines.append, clock=clock, sleep=sleep)
        self.assertTrue(any("probably asleep" in l and "120 minute" in l for l in lines), lines)

    def test_a_stop_wanted_by_the_viewer_reaches_the_worker_once(self):
        proc, wants = self.Proc(), {"n": 0}

        def sleep(_):
            wants["n"] += 1
            if wants["n"] == 3:
                proc.ended, proc.returncode = True, 0
        worker.follow(self.journal, proc=proc, want_stop=lambda: wants["n"] >= 1, sleep=sleep)
        self.assertTrue(worker.read_json(worker.sidecar(self.journal, ".control.json"))["stop"])

    def test_a_worker_that_vanished_without_an_exit_code_returns_none(self):
        proc = self.Proc()
        proc.ended, proc.returncode = True, -9                         # it was killed: the process ended, the status never said how
        self.assertIsNone(worker.follow(self.journal, proc=proc, sleep=lambda _: None))

    def test_a_viewer_without_a_process_handle_uses_the_pid_in_the_status(self):
        worker.write_json(worker.sidecar(self.journal, ".status.json"), {"pid": 2 ** 22 + 999, "state": "running", "exit": None})
        self.assertIsNone(worker.follow(self.journal, sleep=lambda _: None))               # that process is not there

    def test_where_a_resumed_run_begins_in_the_log_is_respected(self):
        log = worker.sidecar(self.journal, ".out")
        log.write_text("old run\n", encoding="utf-8")
        start, lines, proc = log.stat().st_size, [], self.Proc()
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("new run\n")
        proc.ended, proc.returncode = True, 0
        worker.follow(self.journal, proc=proc, on_line=lines.append, out_from=start, sleep=lambda _: None)
        self.assertEqual(lines, ["new run"])


class WhatADeadWorkerLeaves(Folder):
    def test_the_steps_left_in_doubt_are_named(self):
        records = [{"type": "header", "plan_hash": "h"},
                   {"type": "step", "serial": "A1", "step": "0:download", "state": "started"},
                   {"type": "step", "serial": "A1", "step": "0:download", "state": "ok"},
                   {"type": "step", "serial": "A1", "step": "3:install", "state": "started"},
                   {"type": "step", "serial": "A2", "step": "1:backup", "state": "started"},
                   {"type": "step", "serial": "A2", "step": "1:backup", "state": "failed"}]
        self.journal.write_text("\n".join(json.dumps(r) for r in records) + "\n{torn", encoding="utf-8")
        self.assertEqual(worker.in_doubt(self.journal), [("A1", "3:install")])

    def test_a_dead_run_is_reported_and_a_living_one_is_not(self):
        self.journal.write_text(json.dumps({"type": "step", "serial": "A1", "step": "3:install", "state": "started"}) + "\n", encoding="utf-8")
        worker.write_json(worker.lock_file(self.dir), {"pid": 2 ** 22 + 777, "journal": str(self.journal), "started": "t"})
        found = worker.dead_run(self.dir)
        self.assertEqual(found["in_doubt"], [("A1", "3:install")])
        worker.write_json(worker.lock_file(self.dir), {"pid": os.getpid(), "journal": str(self.journal)})
        self.assertIsNone(worker.dead_run(self.dir))


class Starting(Folder):
    def test_a_started_process_outlives_the_call_and_writes_to_the_log(self):
        log = self.dir / "w.out"
        proc = launch.spawn([sys.executable, "-c", "import time; print('hello from the worker', flush=True); time.sleep(0.5)"], str(log))
        self.assertTrue(worker.pid_alive(proc.pid))
        proc.wait(timeout=20)
        self.assertIn("hello from the worker", log.read_text(encoding="utf-8"))

    def test_the_child_can_import_the_same_code(self):
        env = worker.child_env()
        self.assertIn(str(Path(worker.__file__).resolve().parent.parent), env["PYTHONPATH"].split(os.pathsep))


class TheCommandOfTheWorker(unittest.TestCase):
    def args(self, *extra):
        return cli.parse_args(["upgrade", "--source", "fake:fleet.json", "--target", TARGET, "--serial", "A1", "--serial", "A2", "--color", "never",
                               "--ascii", *extra])

    def test_it_repeats_what_was_asked_and_adds_what_makes_it_a_worker(self):
        argv = cli._worker_argv(self.args(), {"approval": "a.json", "journal": "j.jsonl", "resume": False, "fresh": False, "worker": True,
                                              "foreground": False})
        self.assertEqual(argv[:3], worker.python_command())
        self.assertEqual(argv[len(worker.python_command())], "upgrade")
        for expected in (["--target", TARGET], ["--serial", "A1"], ["--serial", "A2"], ["--approval", "a.json"], ["--journal", "j.jsonl"], ["--color", "never"]):
            at = argv.index(expected[0])
            self.assertIn(expected[1], argv[at:at + 2 + 2 * (expected[0] == "--serial")])
        self.assertIn("--worker", argv)
        self.assertIn("--ascii", argv)
        for left_out in ("--resume", "--fresh", "--foreground", "--source"):
            self.assertNotIn(left_out, argv)

    def test_a_change_made_after_parsing_is_carried(self):
        args = self.args()
        args.resume = True                                  # what the screen does when a person chooses to continue an earlier run
        self.assertIn("--resume", cli._worker_argv(args, {"worker": True}))
        argv = cli._worker_argv(args, {"resume": False, "worker": True})
        self.assertNotIn("--resume", argv)

    def test_the_command_it_builds_parses_back_to_the_same_request(self):
        argv = cli._worker_argv(self.args(), {"approval": "a.json", "journal": "j.jsonl", "worker": True})
        back = cli.parse_args(["--source" if False else "upgrade", "--source", "fake:fleet.json"] + argv[len(worker.python_command()) + 1:])
        self.assertEqual((back.target, back.serial, back.approval, back.journal, back.worker), (TARGET, ["A1", "A2"], "a.json", "j.jsonl", True))


class HandingOver(Folder):
    """The viewer side of a real run: with a stand-in for the process start, so nothing is really started."""

    def setUp(self):
        super().setUp()
        self.started = []
        self.real_spawn = launch.spawn
        launch.spawn = self.fake_spawn
        self.addCleanup(lambda: setattr(launch, "spawn", self.real_spawn))

    def fake_spawn(self, argv, log_path, env=None):
        self.started.append((argv, str(log_path)))
        Path(log_path).write_text("worker says hello\nall done\n", encoding="utf-8")
        worker.write_json(worker.sidecar(self.journal, ".status.json"), {"state": "finished", "exit": 0, "pid": 1})
        return types.SimpleNamespace(pid=4242, returncode=0, poll=lambda: 0)

    def args(self):
        return cli.parse_args(["upgrade", "--source", "fake:fleet.json", "--target", TARGET, "--serial", "A1", "--color", "never", "--ascii"])

    def approval(self):
        return approval_mod.Approval("h", ["A1"], [], "tester", "now", "2026-01-01T00:00:00+00:00")

    def bundle(self):
        return types.SimpleNamespace(record=lambda r: None, stop=threading.Event())

    def hand_over(self, args=None, resuming=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._hand_to_worker(args or self.args(), cli._style(args or self.args()), self.bundle(), self.approval(), str(self.journal), resuming)
        return code, out.getvalue()

    def test_the_worker_is_started_with_the_approval_beside_the_journal_and_its_output_is_shown(self):
        code, out = self.hand_over()
        self.assertEqual(code, 0)
        argv, log = self.started[0]
        self.assertIn("--worker", argv)
        self.assertEqual(argv[argv.index("--journal") + 1], str(self.journal))
        approval_file = argv[argv.index("--approval") + 1]
        self.assertEqual(approval_file, str(self.journal) + ".approval.json")
        self.assertEqual(json.loads(Path(approval_file).read_text(encoding="utf-8"))["serials"], ["A1"])
        self.assertTrue(log.endswith(".out"))
        self.assertIn("worker says hello", out)
        self.assertIn("closing this window does not stop it", out)

    def test_an_approval_file_that_was_given_is_used_as_it_is(self):
        args = self.args()
        args.approval = str(self.dir / "given.json")
        self.hand_over(args)
        self.assertEqual(self.started[0][0][self.started[0][0].index("--approval") + 1], str(self.dir / "given.json"))
        self.assertFalse(Path(str(self.journal) + ".approval.json").exists())

    def test_continuing_a_run_says_so_to_the_worker(self):
        self.hand_over(resuming=True)
        self.assertIn("--resume", self.started[0][0])

    def test_a_run_already_going_here_is_refused_before_anything_starts(self):
        worker.acquire(self.dir, {"journal": "the-other"})
        with self.assertRaisesRegex(ValueError, "already in progress"):
            self.hand_over()
        self.assertEqual(self.started, [])

    def test_ctrl_c_in_the_viewer_leaves_the_run_going(self):
        real = worker.follow
        worker.follow = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        try:
            code, out = self.hand_over()
        finally:
            worker.follow = real
        self.assertEqual(code, 0)
        self.assertIn("The run is still going", out)

    def test_a_worker_that_left_no_exit_code_is_said_plainly(self):
        real = worker.follow
        worker.follow = lambda *a, **k: None
        try:
            code, out = self.hand_over()
        finally:
            worker.follow = real
        self.assertEqual(code, 1)
        self.assertIn("without leaving an exit code", out)


class TheWorkerItself(Folder):
    """``upgrade --worker`` run here, against the simulator: lock, heartbeat, status, release."""

    def test_it_runs_takes_the_lock_and_leaves_a_status_and_no_lock(self):
        fleet = self.dir / "fleet.json"
        fleet.write_text(json.dumps(fleet_data(pair("A1", "A2"))), encoding="utf-8")
        plan_out = io.StringIO()
        with contextlib.redirect_stdout(plan_out):
            self.assertEqual(cli.main(["upgrade", "--dry-run", "--source", f"fake:{fleet}", "--target", TARGET, "--journal", str(self.dir / "dry.jsonl"),
                                       "--color", "never"]), 0)
        # a worker is the same command with --worker: it needs an approval for the plan it will re-read
        from tests.test_phase3 import SelectCommandAndDryRun as Scaffold
        scaffold = Scaffold("test_a_dry_run_asks_for_no_approval")
        scaffold.setUp()
        self.addCleanup(scaffold.tmp.cleanup)
        code, out, _ = scaffold._prepare_with_typed_answers(["APPROVE"])
        self.assertEqual(code, 0, out)
        kit = scaffold.dir / "kit"
        approval = next(kit.glob("*.approval.json"))
        serials = next(kit.glob("*.serials.txt"))
        journal = scaffold.dir / "w.jsonl"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["upgrade", "--source", f"fake:{scaffold.fleet_file}", "--target", TARGET, "--serials-file", str(serials),
                             "--approval", str(approval), "--journal", str(journal), "--color", "never", "--worker"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("worker", out.getvalue())
        status = worker.status(journal)
        self.assertEqual((status["state"], status["exit"]), ("finished", 0))
        self.assertFalse(worker.lock_file(journal.parent).exists())

    def test_a_second_worker_in_the_same_folder_is_refused(self):
        from tests.test_phase3 import SelectCommandAndDryRun as Scaffold
        scaffold = Scaffold("test_a_dry_run_asks_for_no_approval")
        scaffold.setUp()
        self.addCleanup(scaffold.tmp.cleanup)
        code, out, _ = scaffold._prepare_with_typed_answers(["APPROVE"])
        kit = scaffold.dir / "kit"
        worker.acquire(scaffold.dir, {"journal": "other"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["upgrade", "--source", f"fake:{scaffold.fleet_file}", "--target", TARGET, "--serials-file", str(next(kit.glob("*.serials.txt"))),
                             "--approval", str(next(kit.glob("*.approval.json"))), "--journal", str(scaffold.dir / "w2.jsonl"), "--color", "never", "--worker"])
        self.assertEqual(code, 3)
        self.assertIn("already in progress", out.getvalue())
        self.assertFalse((scaffold.dir / "w2.jsonl").exists())


class WatchAndStop(Folder):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_watch_with_no_run_says_so(self):
        code, out, _ = self.run_cli("watch", "--runs-dir", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("no run to look at", out)

    def test_watch_shows_the_end_of_a_finished_run_and_its_exit_code(self):
        worker.sidecar(self.journal, ".out").write_text("\n".join(f"line {n}" for n in range(100)) + "\n", encoding="utf-8")
        worker.write_json(worker.sidecar(self.journal, ".status.json"), {"state": "finished", "exit": 1, "pid": 2 ** 22 + 5})
        code, out, _ = self.run_cli("watch", "--runs-dir", str(self.dir), "--lines", "3")
        self.assertEqual(code, 1)
        self.assertIn("line 99", out)
        self.assertNotIn("line 50", out)

    def test_watch_says_when_the_worker_was_ended_abruptly(self):
        self.journal.write_text(json.dumps({"type": "step", "serial": "A1", "step": "3:install", "state": "started"}) + "\n", encoding="utf-8")
        worker.write_json(worker.lock_file(self.dir), {"pid": 2 ** 22 + 6, "journal": str(self.journal), "started": "t"})
        code, out, _ = self.run_cli("watch", "--runs-dir", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("ended abruptly", out)
        self.assertIn("3:install", out)

    def test_stop_asks_the_running_worker_and_says_when_there_is_none(self):
        code, out, _ = self.run_cli("stop", "--runs-dir", str(self.dir))
        self.assertEqual((code, "No run is going on" in out), (1, True))
        worker.acquire(self.dir, {"journal": str(self.journal)})
        code, out, _ = self.run_cli("stop", "--runs-dir", str(self.dir))
        self.assertEqual(code, 0)
        self.assertTrue(worker.read_json(worker.sidecar(self.journal, ".control.json"))["stop"])


class TheShell(unittest.TestCase):
    def test_quitting_stops_what_this_window_runs_but_not_what_a_worker_runs(self):
        from panupgrade import shell
        mine, theirs, plain = cli.StopSwitch(), cli.StopSwitch(), threading.Event()
        theirs.detached = True
        self.assertTrue(shell.Shell._quit(types.SimpleNamespace(stops=[mine, theirs, plain])))
        self.assertEqual((mine.is_set(), theirs.is_set(), plain.is_set()), (True, False, True))

    def test_a_run_that_only_a_viewer_watches_is_not_counted_as_changing_firewalls_from_this_window(self):
        from panupgrade import progress, shell

        def hand_over():
            progress.detached(4242)
            time.sleep(0.3)
        job = shell.Job(hand_over, "Upgrade (x)", changes=True)
        end = time.monotonic() + 3
        while not job.detached and time.monotonic() < end:
            time.sleep(0.01)
        me = types.SimpleNamespace(jobs=[job])
        self.assertEqual(shell.Shell.changing(me), [])
        self.assertEqual(shell.Shell.detached_runs(me), [job])
        end = time.monotonic() + 3
        while job.running and time.monotonic() < end:
            time.sleep(0.01)


def _shell_case():
    from tests.test_shell import ShellCase
    return ShellCase


class TheUpgradeScreen(_shell_case()):
    def lock(self, journal, **status):
        runs = self.lab.path("runs")
        runs.mkdir(parents=True, exist_ok=True)
        worker.write_json(worker.lock_file(runs), {"pid": os.getpid(), "journal": str(runs / journal), "started": "t"})
        if status:
            worker.write_json(worker.sidecar(runs / journal, ".status.json"), status)
        return runs / journal

    def test_the_screen_offers_to_watch_a_run_going_in_the_background_only_when_there_is_one(self):
        ui = self.make()
        screen = ui.screens["upgrade"]
        self.assertNotIn("Watch the run in the background", [label for label, _ in screen.buttons()])
        self.lock("upgrade-1.jsonl")
        screen._background = (0.0, None)                          # do not wait for the two-second memory
        self.assertIn("Watch the run in the background", [label for label, _ in screen.buttons()])

    def test_the_prepare_screen_does_not(self):
        ui = self.make()
        self.lock("upgrade-1.jsonl")
        self.assertNotIn("Watch the run in the background", [label for label, _ in ui.screens["prepare"].buttons()])

    def test_watching_shows_the_worker_output_and_ends_with_its_exit_code(self):
        from tests.test_shell import wait
        ui = self.make()
        screen = ui.screens["upgrade"]
        journal = self.lock("upgrade-1.jsonl", state="finished", exit=0, pid=os.getpid())
        worker.sidecar(journal, ".out").write_text("step one done\nall finished\n", encoding="utf-8")
        screen._background = (0.0, None)
        screen.watch_background()
        self.assertEqual(screen.stage, "running")
        wait(ui)
        self.assertEqual((screen.stage, screen.result), ("done", 0))
        self.assertIn("all finished", screen.lines)

    def test_stop_on_a_watched_run_asks_the_worker_to_stop(self):
        ui = self.make()
        screen = ui.screens["upgrade"]
        screen.watch_stop, screen.stage = cli.StopSwitch(), "running"
        screen.job = types.SimpleNamespace(changes=True)
        self.assertIn("Stop after the steps in progress", [label for label, _ in screen.running_buttons()])
        screen.stop()
        self.assertTrue(screen.watch_stop.is_set())


class AWorkerThatReallyRuns(unittest.TestCase):
    """A real second process: started detached, followed to its end, against the simulator."""

    def test_it_runs_to_the_end_on_its_own_and_the_viewer_sees_it_all(self):
        from tests.test_phase3 import SelectCommandAndDryRun as Scaffold
        scaffold = Scaffold("test_a_dry_run_asks_for_no_approval")
        scaffold.setUp()
        self.addCleanup(scaffold.tmp.cleanup)
        code, out, _ = scaffold._prepare_with_typed_answers(["APPROVE"])
        self.assertEqual(code, 0, out)
        kit = scaffold.dir / "kit"
        journal = scaffold.dir / "real.jsonl"
        argv = worker.python_command() + ["upgrade", "--source", f"fake:{scaffold.fleet_file}", "--target", TARGET, "--serials-file",
                                          str(next(kit.glob("*.serials.txt"))), "--approval", str(next(kit.glob("*.approval.json"))),
                                          "--journal", str(journal), "--color", "never", "--worker"]
        log = worker.sidecar(journal, ".out")
        proc = launch.spawn(argv, log, env=worker.child_env())
        lines, records = [], []
        code = worker.follow(journal, proc=proc, on_line=lines.append, on_record=records.append, interval=0.2)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertTrue([line for line in lines if "worker" in line and "started" in line])
        self.assertEqual({r["serial"] for r in records if r.get("type") == "step"}, {"A1", "A2"})
        status = worker.status(journal)
        self.assertEqual((status["state"], status["exit"]), ("finished", 0))
        self.assertFalse(worker.lock_file(journal.parent).exists())
        self.assertFalse(worker.pid_alive(proc.pid))


class TheSetting(unittest.TestCase):
    def test_the_default_is_to_carry_on_unwatched(self):
        s = settings.load()
        self.assertEqual((s.unattended, s.unattended_pause_after_min), ("continue", 15))

    def test_pause_is_a_choice_and_nothing_else_is(self):
        self.assertEqual(settings.load(overrides=["unattended=pause"]).unattended, "pause")
        with self.assertRaises(settings.SettingsError):
            settings.load(overrides=["unattended=maybe"])
        with self.assertRaises(settings.SettingsError):
            settings.load(overrides=["unattended_pause_after_min=0"])


if __name__ == "__main__":
    unittest.main()
