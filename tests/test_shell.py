# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The one-screen workflow: home, hotkeys between steps, background reads, and the way back from a sign-in problem."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from panupgrade import cli, credstore, prepare as prepare_mod, profiles, progress, settings, shell, site, trust, tui
from panupgrade.trust import CertificateProblem
from panupgrade.model import DeviceRef
from panupgrade.sources import SourceError
from tests.test_auth import Home


def ref(serial, name, version="10.2.1"):
    return DeviceRef(serial=serial, hostname=name, model="PA-VM", sw_version=version, connected=True, device_group="lab", ha_state="active")


REFS = [ref("S1", "fw-one"), ref("S2", "fw-two")]


class FakeUpgrade:
    """Stands in for what the review of an upgrade finds."""

    def __init__(self, ready=("A2", "A1"), blocked=(), previous=None, label="LIVE"):
        names = {"A1": "fw-one", "A2": "fw-two", "B1": "fw-three", "B2": "fw-four"}
        steps = [{"action": "backup", "traffic": "none", "note": ""}, {"action": "install 10.2.3", "traffic": "none", "note": ""}]
        devices = [{"serial": x, "hostname": names[x], "ha_state": "active" if x == "A1" else "passive", "current_version": "10.2.1", "verdict": "ready",
                    "steps": steps} for x in ready]
        devices += [{"serial": x, "hostname": names[x], "ha_state": "", "current_version": "10.2.1", "verdict": "blocked"} for x in blocked]
        self.plan = {"hash": "cd" * 32, "target": "10.2.3", "devices": devices, "settings": {"max_parallel": 5},
                     "summary": {"ready": len(ready), "caution": 0, "blocked": len(blocked), "skipped": 0, "outage_steps": 0, "failover_steps": 1},
                     "order": ([[x for x in ready if x.startswith("A")]] + [[x] for x in ready if not x.startswith("A")]) if ready else [],
                     "cautions": [{"scope": "A1", "code": "failover_band", "text": "sessions may move"}] if ready else []}
        self.label, self.previous, self.created = label, previous, time.time()
        self.stop = threading.Event()
        self.events = []

    def record(self, record):
        self.events.append((time.time(), record))

    def events_snapshot(self):
        return list(self.events)

    def step(self, serial, step, state, **detail):
        self.record({"type": "step", "serial": serial, "step": step, "state": state, "detail": detail})

    def ready(self):
        return [d for d in self.plan["devices"] if d["verdict"] in ("ready", "caution")]

    def acks(self):
        return [f"{c['scope']}:{c['code']}" for c in self.plan["cautions"]]


class FakeBundle:
    """Stands in for what the review of a prepare finds. The shell tests never reach a real Panorama or firewall."""

    def __init__(self, serials=("S1", "S2"), content=False):
        self.plan = {"hash": "ab" * 32, "serials": list(serials),
                     "units": [{"active": "S1", "active_name": "fw-one", "passive": "S2", "passive_name": "fw-two", "content": ["content"] if content else [],
                                "sync_config": True, "content_diffs": [], "content_reason": ""}] if serials else [],
                     "downloads": [{"serial": "S1", "hostname": "fw-one", "version": "10.2.3"}] if serials else [], "content_tasks": []}
        self.label, self.created = "LIVE", time.time()
        self.stop = threading.Event()
        self.groups = prepare_mod.work_groups(self.plan)
        self.events, self._lock = [], threading.Lock()

    def step(self, serial, step, state, **detail):
        """One journal record, as the run would report it."""
        with self._lock:
            self.events.append((time.time(), {"type": "step", "serial": serial, "step": step, "state": state, "detail": detail}))

    def events_snapshot(self):
        with self._lock:
            return list(self.events)


def go(ui, key):
    """Open a screen by its hotkey and, if it is waiting to be started, press Start (opening a screen never starts anything by itself)."""
    ui.press(key)
    screen = ui.screen
    if isinstance(screen, shell.JobScreen):
        idle = screen.record() is None and screen.reading() is None and not screen.auto
    elif isinstance(screen, shell.ChangeScreen):
        idle = screen.stage == "idle"
    else:
        idle = False
    if idle:
        ui.press("enter")


def wait(ui, seconds=3):
    """Let background work finish, the way the terminal loop's ticks would."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ui.tick()
        if not any(job.running or not job.reported for job in ui.jobs):
            return
        time.sleep(0.01)
    raise AssertionError("the background work did not finish")


class ShellCase(Home):
    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)                              # upgradeList.txt and settings.toml land here
        self.addCleanup(os.chdir, self.cwd)
        patcher = mock.patch("panupgrade.userenv.supported", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lab = profiles.create("lab", "192.0.2.1")
        credstore.write(self.lab.creds_file, {"tool": "pw"})
        self.commands, self.reads, self.reviews, self.executed = [], [], [], []
        self.up_reviews, self.up_executed = [], []

    def make(self, address="192.0.2.1", target="12.1.10", run=None, read=None, review=None, execute=None, up_review=None, up_execute=None, read_versions=None):
        def run_command(args):
            self.commands.append((args.command, getattr(args, "target", None)))
            return run(args) if run else (print(f"output of {args.command}") or 0)

        def read_devices(args):
            self.reads.append(args.command)
            return read(args) if read else REFS
        def prepare_review(args):
            self.reviews.append(args.target)
            return review(args) if review else (print("PREPARE PLAN TEXT: 2 config syncs") or FakeBundle())

        def prepare_execute(args, bundle, approve):
            approval = approve(None) if approve else None
            self.executed.append((bundle, approval))
            return execute(args, bundle, approve) if execute else (print("PREPARE RAN") or 0)
        def upgrade_review(args):
            self.up_reviews.append(args)
            return up_review(args) if up_review else (print("UPGRADE PLAN TEXT: 2 ready") or FakeUpgrade())

        def upgrade_execute(args, bundle, approve):
            approval = approve(None) if approve else None
            self.up_executed.append((args, bundle, approval))
            return up_execute(args, bundle, approve) if up_execute else (print("UPGRADE RAN") or 0)
        session = shell.Session(cli.parse_args(["shell"]), address, target, self.lab)
        ui = shell.Shell(session, run_command=run_command, read_devices=read_devices, prepare_review=prepare_review, prepare_execute=prepare_execute,
                         upgrade_review=upgrade_review, upgrade_execute=upgrade_execute, read_versions=read_versions)
        self.addCleanup(self.settle, ui)
        return ui

    @staticmethod
    def settle(ui, seconds=5):
        """No background job outlives its test: a late one would write into whatever the next test is checking."""
        end = time.monotonic() + seconds
        while any(job.running for job in ui.jobs) and time.monotonic() < end:
            time.sleep(0.01)

    def text(self, ui, width=110, height=30):
        return tui.plain(ui.render(width, height))


class Home_(ShellCase):
    def test_it_opens_on_the_home_screen_with_the_steps_in_order_and_where_each_stands(self):
        ui = self.make()
        text = self.text(ui)
        for needle in ("Panorama profile", "< lab >", "Panorama address", "[192.0.2.1]", "Target PAN-OS", "1  Sign in", "2  Choose firewalls", "3  Fleet report", "4  Plan the upgrade",
                       "5  Prepare", "6  Upgrade", "7  Settings", "8  PAN-OS releases", "9  Jobs", "Q  Quit", "Alt-R report", "Alt-J jobs"):
            self.assertIn(needle, text)
        self.assertIn("tool (password) reads", text)
        self.assertIn("built-in defaults", text)

    def test_the_address_and_target_are_typed_on_the_home_screen(self):
        ui = self.make(address="", target="")
        ui.press("down")                                       # off the profile row, onto the address
        for ch in "pano.example":
            ui.press(ch)
        ui.press("enter")
        for ch in "10.2.11-h1":
            ui.press(ch)
        self.assertEqual((ui.session.address, ui.session.target), ("pano.example", "10.2.11-h1"))
        ui.press("backspace")
        ui.press("ctrl-u")
        self.assertEqual(ui.session.target, "")

    def test_enter_opens_a_step_and_escape_or_q_leaves(self):
        ui = self.make()
        for _ in range(6):                                    # profile, address, target, the picker, then step 1 (sign in), 2 (firewalls) and 3: the fleet report
            ui.press("down")
        ui.press("enter")
        self.assertEqual(ui.screen.name, "report")
        wait(ui)
        ui.press("esc")
        self.assertEqual(ui.screen.name, "home")
        self.assertEqual(ui.press("esc"), "accept")           # esc on home quits
        ui2 = self.make()
        ui2.screen.at = ui2.screen.TOP + len(shell.STEP_ORDER)
        self.assertEqual(ui2.press("enter"), "accept")

    def test_q_quits_from_the_step_list_but_is_just_a_letter_in_a_text_field(self):
        ui = self.make()
        ui.press("down")                                       # onto the address
        ui.press("q")
        self.assertEqual(ui.session.address, "192.0.2.1q")
        ui.screen.at = ui.screen.TOP
        self.assertEqual(ui.press("q"), "accept")


class SeveralPanoramas(ShellCase):
    def setUp(self):
        super().setUp()
        self.west = profiles.create("west", "10.0.0.2")
        self.west.target = "11.1.4"
        self.west.save()
        credstore.write(self.west.creds_file, {"w-admin": "pw"})

    def test_left_and_right_on_the_profile_row_switch_panorama_with_its_address_and_target(self):
        ui = self.make(target="10.2.11-h1")
        ui.press("right")
        self.assertEqual((ui.session.profile.name, ui.session.address, ui.session.target), ("west", "10.0.0.2", "11.1.4"))
        self.assertIn("Now working in the profile west", ui.message)
        text = self.text(ui)
        self.assertIn("< west >", text)
        self.assertIn("2 of 2", text)
        self.assertIn("w-admin (password) reads", text)
        ui.press("left")
        self.assertEqual((ui.session.profile.name, ui.session.address), ("lab", "192.0.2.1"))
        self.assertEqual(profiles.remembered(), "lab")

    def test_what_was_read_for_one_panorama_is_forgotten_when_switching_to_another(self):
        ui = self.make(run=lambda args: print("report") or 0)
        go(ui, "alt-r"); wait(ui)
        self.assertIn("read at", self.text(ui.goto("home") or ui))
        ui.press("right")
        self.assertIn("not read yet", self.text(ui))
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(len(self.commands), 2)                   # it read again, for the other Panorama

    def test_a_step_runs_in_the_profile_in_use(self):
        seen = []
        ui = self.make(run=lambda args: seen.append((args._profile.name, args.source)) or 0)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-h"); ui.press("right")
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(seen, [("lab", "panorama:192.0.2.1"), ("west", "panorama:10.0.0.2")])

    def test_the_address_and_target_typed_are_kept_in_the_profile_not_in_a_shared_file(self):
        ui = self.make()
        ui.press("down")
        ui.press("ctrl-u")
        for ch in "192.0.2.9":
            ui.press(ch)
        ui.press("enter")
        ui.press("ctrl-u")
        for ch in "12.1.10":
            ui.press(ch)
        go(ui, "alt-r")                                        # leaving the screen keeps them
        loaded = profiles.load("lab")
        self.assertEqual((loaded.address, loaded.target), ("192.0.2.9", "12.1.10"))
        self.assertEqual(profiles.load("west").target, "11.1.4")  # the other Panorama is untouched
        self.assertEqual(site.read_state().get("panorama"), None)

    def test_without_a_profile_what_is_typed_is_remembered_for_next_time_as_before(self):
        session = shell.Session(cli.parse_args(["shell"]), "pano.example", "10.2.11-h1", None)
        ui = shell.Shell(session, run_command=lambda a: 0, read_devices=lambda a: REFS)
        ui.goto("report")
        self.assertEqual(site.read_state(), {"panorama": "pano.example", "target": "10.2.11-h1"})
        self.assertIn("< none >", self.text(ui.goto("home") or ui))
        self.assertIn("no Panorama profile yet", self.text(ui))

    def test_enter_on_the_profile_row_opens_the_screen_that_manages_profiles(self):
        ui = self.make()
        ui.press("enter")
        self.assertEqual(ui.screen.name, "auth")
        self.assertIn("Panorama profile", self.text(ui))

    def test_the_first_visit_with_no_profile_goes_straight_to_making_one_with_the_address_filled_in(self):
        for p in profiles.names():
            profiles.remove(p)
        session = shell.Session(cli.parse_args(["shell"]), "192.0.2.77", "", None)
        ui = shell.Shell(session, run_command=lambda a: 0, read_devices=lambda a: REFS)
        ui.press("enter")
        self.assertEqual(ui.screen.name, "auth")
        self.assertEqual(ui.screen.form.modal["type"], "profile")
        self.assertIn("New Panorama profile", self.text(ui))

    def test_the_command_line_starts_in_the_profile_it_names(self):
        seen = {}
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=lambda ui, title="": seen.update(p=ui.session.profile.name, a=ui.session.address,
                                                                                          t=ui.session.target) or True):
            cli.main(["shell", "--profile", "west"])
        self.assertEqual(seen, {"p": "west", "a": "10.0.0.2", "t": "11.1.4"})
        self.assertEqual(profiles.remembered(), "west")

    def test_an_unknown_profile_on_the_command_line_is_an_error(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.main(["shell", "--profile", "nope"]) if False else cli.parse_args(["shell", "--profile", "nope"])
        self.assertIn("", err.getvalue())


class LeavingWithCtrlC(ShellCase):
    def test_ctrl_c_leaves_from_any_screen(self):
        for name in ("home", "report", "settings", "auth"):
            ui = self.make()
            ui.goto(name)
            self.assertEqual(ui.press("ctrl-c"), "cancel", name)


class Hotkeys(ShellCase):
    def test_each_alt_key_goes_to_its_screen_from_anywhere(self):
        ui = self.make()
        for key, name in (("alt-a", "auth"), ("alt-r", "report"), ("alt-f", "select"), ("alt-p", "plan"), ("alt-s", "settings"), ("alt-h", "home")):
            ui.press(key)
            self.assertEqual(ui.screen.name, name, key)

    def test_the_numbered_keys_follow_the_steps_in_order(self):
        ui = self.make()
        for number, name in enumerate(shell.STEP_ORDER, 1):
            ui.press(f"alt-{number}")
            self.assertEqual(ui.screen.name, name)
        ui.press("alt-0")
        self.assertEqual(ui.screen.name, shell.STEP_ORDER[-1])        # no step 0 or 10: nothing happens
        ui.press("alt-10")
        self.assertEqual(ui.screen.name, shell.STEP_ORDER[-1])
        ui.press("alt-z")
        self.assertEqual(ui.screen.name, shell.STEP_ORDER[-1])

    def test_a_screen_with_unsaved_changes_asks_before_the_hotkey_throws_them_away(self):
        ui = self.make()
        ui.press("alt-s")
        form = ui.screen.form
        form.zone, form.focus = "rows", form.names.index("max_parallel")
        form.press("backspace"); form.press("3")
        self.assertTrue(ui.screen.dirty())
        go(ui, "alt-r")
        self.assertEqual(ui.screen.name, "settings")
        self.assertIn("unsaved changes", ui.message)
        go(ui, "alt-r")
        self.assertEqual(ui.screen.name, "report")

    def test_every_screen_shows_the_hotkeys_in_its_last_line(self):
        ui = self.make()
        for name in shell.STEP_ORDER + ["home"]:
            ui.goto(name)
            lines = ui.render(110, 24)
            self.assertEqual(len(lines), 24)
            self.assertIn("Alt-H home", tui.plain(lines[-1:]))


class TheFleetReport(ShellCase):
    def test_opening_it_reads_in_the_background_and_shows_the_output_and_home_remembers(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("Fleet report for x\nNeeds attention (7 firewalls)") or 0)
        go(ui, "alt-r")
        self.assertIsNotNone(ui.tick_s)                        # work is going on: the loop should poll
        self.assertIn("working", self.text(ui))
        gate.set()
        wait(ui)
        self.assertIsNone(ui.tick_s)
        text = self.text(ui)
        self.assertIn("Fleet report for x", text)
        self.assertIn("finished in", text)
        self.assertEqual(self.commands, [("report", None)])
        ui.press("alt-h")
        self.assertIn("7 firewalls need attention", self.text(ui))

    def test_refresh_reads_again_and_going_away_and_back_does_not(self):
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-h"); go(ui, "alt-r")
        self.assertEqual(len(self.commands), 1)
        ui.press("r"); wait(ui)
        self.assertEqual(len(self.commands), 2)

    def test_a_different_address_reads_again(self):
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-h")
        ui.session.address = "192.0.2.9"
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(len(self.commands), 2)

    def test_without_an_address_it_says_where_to_type_one_and_reads_nothing(self):
        ui = self.make(address="")
        go(ui, "alt-r")
        self.assertIn("type its address, on the home screen", self.text(ui))
        self.assertEqual(self.commands, [])

    def test_the_action_bar_changes_sign_in_or_goes_home(self):
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        ui.press("right"); ui.press("enter")
        self.assertEqual(ui.screen.name, "auth")
        self.assertEqual(ui.after_sign_in, "report")
        go(ui, "alt-r"); wait(ui)
        ui.press("right"); ui.press("right"); ui.press("enter")
        self.assertEqual(ui.screen.name, "home")

    def test_long_output_scrolls(self):
        ui = self.make(run=lambda args: print("\n".join(f"line {i}" for i in range(100))) or 0)
        go(ui, "alt-r"); wait(ui)
        self.assertNotIn("line 90", self.text(ui, 110, 20))
        ui.press("end")
        self.assertIn("line 99", self.text(ui, 110, 20))
        ui.press("home")
        self.assertIn("line 0", self.text(ui, 110, 20))
        self.assertIn("of 100", self.text(ui, 110, 20))

    def test_an_error_is_shown_not_hidden(self):
        ui = self.make(run=lambda args: print("error: Panorama said no", file=__import__("sys").stderr) or 2)
        go(ui, "alt-r"); wait(ui)
        text = self.text(ui)
        self.assertIn("Panorama said no", text)
        self.assertIn("with an error", text)

    def test_a_bug_in_a_step_shows_up_on_the_screen_instead_of_ending_the_program(self):
        def boom(args):
            raise RuntimeError("kaboom")
        ui = self.make(run=boom)
        go(ui, "alt-r"); wait(ui)
        self.assertIn("internal error: RuntimeError: kaboom", self.text(ui))

    def test_trouble_a_person_can_act_on_is_reported_plainly(self):
        def trouble(args):
            raise SourceError("cannot reach 192.0.2.1")
        ui = self.make(run=trouble)
        go(ui, "alt-r"); wait(ui)
        self.assertIn("error: cannot reach 192.0.2.1", self.text(ui))
        self.assertNotIn("Traceback", self.text(ui))


class ThePlan(ShellCase):
    def test_it_needs_a_target_and_passes_it_on(self):
        ui = self.make(target="")
        go(ui, "alt-p")
        self.assertIn("Type the target PAN-OS version", self.text(ui))
        self.assertEqual(self.commands, [])
        ui.press("alt-h"); ui.session.target = "10.2.11-h1"
        go(ui, "alt-p"); wait(ui)
        self.assertEqual(self.commands, [("plan", "10.2.11-h1")])
        self.assertIn("output of plan", self.text(ui))
        ui.press("alt-h")
        self.assertIn("last run", self.text(ui))


class SigningInAgain(ShellCase):
    def test_a_step_that_cannot_sign_in_takes_you_to_the_sign_in_screen_and_back_when_it_is_fixed(self):
        calls = []

        def run(args):
            calls.append(1)
            if len(calls) == 1:
                args._sign_in_problem = "there is no usable account"
                return 2
            print("report after signing in")
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        self.assertIn("there is no usable account", self.text(ui))
        form = ui.screen.form
        form.zone = "accounts"
        for key in ("r", "ctrl-d"):                          # use tool for reading, Save & Exit
            ui.press(key)
        self.assertEqual(ui.screen.name, "report")           # straight back to the step ...
        wait(ui)
        self.assertEqual(len(calls), 2)                      # ... which has read again
        self.assertIn("report after signing in", self.text(ui))

    def test_giving_up_on_the_sign_in_screen_goes_home_not_round_in_circles(self):
        ui = self.make(run=lambda args: setattr(args, "_sign_in_problem", "no account") or 2)
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        ui.press("esc")
        self.assertEqual(ui.screen.name, "home")

    def test_the_sign_in_screen_has_the_address_filled_in_for_testing(self):
        ui = self.make()
        ui.press("alt-a")
        self.assertEqual(ui.screen.form.address, "192.0.2.1")

    def test_inside_the_shell_the_command_line_helper_records_the_problem_instead_of_opening_a_second_screen(self):
        args = cli.parse_args(["report", "--panorama", "192.0.2.1"])
        args._in_shell = True
        with mock.patch("panupgrade.cli._run_auth_screen") as screen:
            self.assertFalse(cli._offer_sign_in_screen(args, credstore.NeedsSignIn("no file")))
        screen.assert_not_called()
        self.assertEqual(args._sign_in_problem, "no file")


class AnUnverifiableCertificate(ShellCase):
    problem = CertificateProblem("192.0.2.1", "self-signed certificate", "ab" * 32)

    def test_a_step_that_meets_one_asks_on_the_sign_in_screen_and_reads_again_once_it_is_trusted(self):
        calls = []

        def run(args):
            calls.append(1)
            if len(calls) == 1:
                args._certificate_problem = self.problem
                return 2
            print("report after trusting")
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        text = self.text(ui)
        self.assertIn("Cannot verify Panorama's certificate", text)
        self.assertIn("[ Accept risk & continue ]", text)
        self.assertIn(trust.pretty("ab" * 32)[:20], text)
        ui.press("enter")                                    # accept
        self.assertEqual(ui.screen.name, "report")
        wait(ui)
        self.assertEqual(trust.pinned("192.0.2.1"), "ab" * 32)
        self.assertEqual(len(calls), 2)
        self.assertIn("report after trusting", self.text(ui))

    def test_declining_trusts_nothing_and_does_not_loop(self):
        ui = self.make(run=lambda args: setattr(args, "_certificate_problem", self.problem) or 2)
        go(ui, "alt-r"); wait(ui)
        ui.press("right"); ui.press("enter")                 # Cancel
        self.assertEqual(ui.screen.name, "auth")             # still on the sign-in screen, no certificate pinned
        self.assertIsNone(trust.pinned("192.0.2.1"))
        ui.press("alt-h")
        self.assertEqual(ui.screen.name, "home")

    def test_choosing_firewalls_gets_the_same_recovery(self):
        def read(args):
            args._certificate_problem = self.problem
            raise SourceError("certificate")
        ui = self.make(read=read)
        ui.press("alt-f"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        self.assertEqual(ui.after_sign_in, "select")
        self.assertIn("Accept risk & continue", self.text(ui))


class ChoosingFirewalls(ShellCase):
    def test_the_list_is_read_in_the_background_then_the_form_opens_and_saving_writes_the_file(self):
        gate = threading.Event()
        ui = self.make(read=lambda args: gate.wait(3) and REFS)
        ui.press("alt-f")
        self.assertIn("reading the firewall list", self.text(ui))
        gate.set()
        wait(ui)
        self.assertIn("AVAILABLE", self.text(ui))
        for key in ("down", "a", "ctrl-d"):                  # into Available, add all, Save & Exit
            ui.press(key)
        self.assertEqual(ui.screen.name, "home")
        self.assertEqual(cli._read_list(self.lab.path("upgradeList.txt")), ["S1", "S2"])
        self.assertIn("2 firewalls in upgradeList.txt", self.text(ui))

    def test_a_list_that_cannot_be_read_is_explained_and_enter_tries_again(self):
        attempts = []

        def read(args):
            attempts.append(1)
            if len(attempts) == 1:
                raise SourceError("cannot reach 192.0.2.1")
            return REFS
        ui = self.make(read=read)
        ui.press("alt-f"); wait(ui)
        self.assertIn("cannot reach 192.0.2.1", self.text(ui))
        ui.press("enter"); wait(ui)
        self.assertIn("AVAILABLE", self.text(ui))

    def test_without_an_address_it_says_so(self):
        ui = self.make(address="")
        ui.press("alt-f")
        self.assertIn("type its address", self.text(ui))
        self.assertEqual(self.reads, [])

    def test_a_sign_in_problem_goes_to_the_sign_in_screen(self):
        ui = self.make(read=lambda args: setattr(args, "_sign_in_problem", "no account") or (_ for _ in ()).throw(SourceError("no account")))
        ui.press("alt-f"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        self.assertEqual(ui.after_sign_in, "select")


class OtherSteps(ShellCase):
    def test_every_step_is_on_the_screen_now(self):
        ui = self.make()
        for name in shell.STEP_ORDER:
            ui.goto(name)
            self.assertNotIn("not on the screen yet", self.text(ui))

    def test_settings_open_in_the_shell_and_save_to_settings_toml(self):
        ui = self.make()
        ui.press("alt-s")
        form = ui.screen.form
        form.zone, form.focus = "rows", form.names.index("max_parallel")
        form.press("backspace"); form.press("2")
        ui.press("ctrl-d")
        self.assertEqual(ui.screen.name, "home")
        self.assertIn("max_parallel = 2", self.lab.path("settings.toml").read_text(encoding="utf-8"))

    def test_the_release_table_needs_no_panorama(self):
        ui = self.make(address="", run=lambda args: print("PAN-OS releases") or 0)
        ui.press("alt-8"); wait(ui)
        self.assertIn("PAN-OS releases", self.text(ui))
        self.assertEqual(self.commands, [("releases", None)])


class Drawing(ShellCase):
    def test_no_screen_overflows_any_window(self):
        ui = self.make()
        for name in shell.STEP_ORDER + ["home"]:
            ui.goto(name)
            try:
                wait(ui)
            except AssertionError:
                pass
            for width, height in ((60, 14), (80, 24), (120, 40)):
                lines = ui.render(width, height)
                self.assertEqual(len(lines), height, (name, width, height))
                for line in lines:
                    self.assertLess(len("".join(t for _s, t in line)), width, (name, width, height))


class Launching(Home):
    def test_plain_panupgrade_opens_the_shell_at_a_terminal_and_the_overview_otherwise(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._shell", return_value=0) as opened:
            self.assertEqual(cli.main([]), 0)
        opened.assert_called_once()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("panupgrade help", out.getvalue())

    def test_the_shell_command_needs_a_terminal(self):
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cli.main(["shell"]), 2)
        self.assertIn("needs a terminal", err.getvalue())

    def test_the_address_and_target_are_remembered_for_next_time(self):
        seen = {}

        def fake_show(ui, title=""):
            seen["address"], seen["target"] = ui.session.address, ui.session.target
            ui.session.address, ui.session.target = "pano.example", "10.2.11-h1"
            return True
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=fake_show):
            cli.main(["shell"])
            self.assertEqual(seen, {"address": "", "target": ""})
            cli.main(["shell"])
        self.assertEqual(seen, {"address": "pano.example", "target": "10.2.11-h1"})
        self.assertEqual(site.read_state(), {"panorama": "pano.example", "target": "10.2.11-h1"})

    def test_options_on_the_command_line_win_over_what_was_remembered(self):
        site.write_state(panorama="old.example", target="9.9.9")
        seen = {}
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=lambda ui, title="": seen.update(a=ui.session.address, t=ui.session.target) or True):
            cli.main(["shell", "--panorama", "new.example"])
        self.assertEqual(seen, {"a": "new.example", "t": "9.9.9"})


class TheTerminalLoop(unittest.TestCase):
    class Form:
        tick_s = 0.5

        def __init__(self, changes):
            self.drawn, self.ticks, self.changes, self.keys = 0, 0, changes, []

        def render(self, width, height, title=""):
            self.drawn += 1
            return [[("", "x")]]

        def tick(self):
            self.ticks += 1
            return self.changes

        def press(self, key):
            self.keys.append(key)
            return "accept" if key == "enter" else None

    def run_loop(self, form, script):
        presses = iter(script)
        timeouts = []

        def read_key(timeout=None):
            timeouts.append(timeout)
            return next(presses)
        out = io.StringIO()
        result = tui.run(form, read_key, out, lambda: (80, 24))
        return result, timeouts

    def test_an_idle_pass_calls_tick_and_redraws_only_when_it_says_something_changed(self):
        quiet = self.Form(changes=False)
        result, timeouts = self.run_loop(quiet, [None, None, "enter"])
        self.assertTrue(result)
        self.assertEqual((quiet.ticks, quiet.drawn), (2, 1))          # drawn once at the start, not again after the quiet ticks
        self.assertEqual(timeouts, [0.5, 0.5, 0.5])
        busy = self.Form(changes=True)
        self.run_loop(busy, [None, None, "enter"])
        self.assertEqual(busy.drawn, 3)                                # and again after each tick that reported a change

    def test_a_form_without_background_work_is_read_without_a_timeout(self):
        class Plain:
            def render(self, w, h, t=""):
                return [[]]

            def press(self, key):
                return "accept"
        _, timeouts = self.run_loop(Plain(), ["enter"])
        self.assertEqual(timeouts, [None])


class AltKeys(unittest.TestCase):
    def test_windows_alt_letters_and_digits(self):
        self.assertEqual(tui.translate_windows_key(0x52, "r", 0x02), "alt-r")
        self.assertEqual(tui.translate_windows_key(0x53, chr(0), 0x02), "alt-s")
        self.assertEqual(tui.translate_windows_key(0x35, "5", 0x02), "alt-5")
        self.assertEqual(tui.translate_windows_key(0x52, "r", 0), "r")

    def test_altgr_is_a_way_to_type_a_character_not_a_hotkey(self):
        self.assertEqual(tui.translate_windows_key(0x51, "@", 0x01 | 0x08), "@")

    @unittest.skipIf(os.name == "nt", "select() on a pipe is a Unix thing")
    def test_unix_terminals_send_alt_letter_as_escape_then_the_letter(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b"\x1br")
        self.assertEqual(tui._read_key_posix(read), "alt-r")
        os.write(write, b"\x1b")
        self.assertEqual(tui._read_key_posix(read), "esc")
        self.assertIsNone(tui._read_key_posix(read, timeout=0.01))


class Painting(unittest.TestCase):
    def test_every_style_a_screen_uses_can_be_painted_and_an_unknown_one_degrades_to_plain_text(self):
        class Screen:
            def render(self, w, h, title=""):
                return [[("ok", "fine")], [("nonsense", "still shown")], [("warn", "w"), ("dim", "d"), ("error", "e")]]
        out = io.StringIO()
        tui.draw(Screen(), out, 80, 24)
        text = out.getvalue()
        self.assertIn("fine", text)
        self.assertIn("still shown", text)
        self.assertNotIn("nonsense", text)

    def test_the_real_screens_only_use_styles_the_painter_knows(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"PANUPGRADE_HOME": folder}):
            session = shell.Session(cli.parse_args(["shell"]), "192.0.2.1", "10.2.11-h1")
            ui = shell.Shell(session, run_command=lambda a: 0, read_devices=lambda a: REFS)
            known = set(tui.CODES)
            for name in shell.STEP_ORDER + ["home"]:
                ui.goto(name)
                for line in ui.render(110, 30):
                    for style, _text in line:
                        self.assertIn(style, known, f"{name} uses the style {style!r}")


class TheStepsAreInTheOrderYouDoThem(ShellCase):
    def test_choosing_the_firewalls_comes_before_reading_the_fleet(self):
        self.assertLess(shell.STEP_ORDER.index("select"), shell.STEP_ORDER.index("report"))
        lines = self.text(self.make()).splitlines()
        at = lambda needle: next(i for i, line in enumerate(lines) if needle in line)
        self.assertLess(at("Choose firewalls"), at("Fleet report"))
        hints = self.text(self.make()).splitlines()[-1]
        self.assertLess(hints.index("Alt-F firewalls"), hints.index("Alt-R report"))


class TheLastAnswerStaysPut(ShellCase):
    def test_leaving_the_report_and_coming_back_shows_the_same_answer_not_a_new_read(self):
        ui = self.make(run=lambda args: print("Fleet report\nNeeds attention (3 firewalls)") or 0)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-a"); ui.press("esc")                    # hop to the sign-in screen and home again
        ui.press("alt-f"); ui.press("alt-h")
        go(ui, "alt-r")
        text = self.text(ui)
        self.assertIn("Needs attention (3 firewalls)", text)
        self.assertIn("result from", text)
        self.assertIn("just now", text)
        self.assertEqual(len(self.commands), 1)

    def test_leaving_while_it_is_still_reading_the_answer_is_waiting_when_you_come_back(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("Needs attention (2 firewalls)") or 0)
        go(ui, "alt-r")
        ui.press("alt-h")                                     # off to the home screen while Panorama is still being asked
        gate.set()
        wait(ui)                                              # the loop's ticks run on whatever screen is in front
        self.assertIn("2 firewalls need attention", self.text(ui))
        go(ui, "alt-r")
        self.assertIn("Needs attention (2 firewalls)", self.text(ui))
        self.assertEqual(len(self.commands), 1)

    def test_a_job_that_finishes_between_two_checks_still_gets_its_answer_without_a_key_press(self):
        ui = self.make()
        go(ui, "alt-r")
        end = time.monotonic() + 3
        while ui.jobs[0].running and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(ui.jobs[0].running)
        self.assertIsNotNone(ui.tick_s)                       # the loop must still wake up to collect it ...
        ui.tick()
        self.assertIn("output of report", self.text(ui))      # ... and then it has
        self.assertIsNone(ui.tick_s)

    def test_refreshing_keeps_the_old_answer_on_show_until_the_new_one_arrives(self):
        gate = threading.Event()
        runs = []

        def run(args):
            runs.append(1)
            if len(runs) == 2:
                gate.wait(3)
            print(f"answer number {len(runs)}")
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); wait(ui)
        ui.press("r")
        text = self.text(ui)
        self.assertIn("answer number 1", text)
        self.assertIn("stays on show", text)
        gate.set(); wait(ui)
        self.assertIn("answer number 2", self.text(ui))
        self.assertNotIn("answer number 1", self.text(ui))

    def test_asking_twice_while_it_reads_does_not_start_a_second_read(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("x") or 0)
        go(ui, "alt-r"); ui.press("r"); ui.press("r")
        gate.set(); wait(ui)
        self.assertEqual(len(self.commands), 1)

    def test_each_profile_keeps_its_own_answer_and_switching_back_shows_it_again(self):
        other = profiles.create("other", "192.0.2.77")
        credstore.write(other.creds_file, {"t": "pw"})
        ui = self.make(run=lambda args: print(f"report for {args.profile}") or 0)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-h")
        ui.screen.at = 0
        ui.press("right")                                      # on to the next profile
        go(ui, "alt-r"); wait(ui)
        self.assertIn("report for other", self.text(ui))
        ui.press("alt-h"); ui.screen.at = 0; ui.press("left")
        go(ui, "alt-r")
        self.assertIn("report for lab", self.text(ui))
        self.assertEqual(len(self.commands), 2)

    def test_a_sign_in_problem_found_while_you_were_elsewhere_is_dealt_with_when_you_return(self):
        gate = threading.Event()

        def run(args):
            gate.wait(3)
            args._sign_in_problem = "no usable account"
            return 2
        ui = self.make(run=run)
        go(ui, "alt-r"); ui.press("alt-h")
        gate.set(); wait(ui)
        self.assertEqual(ui.screen.name, "home")                # nobody is yanked away from what they were doing
        self.assertIn("sign in again", ui.render(110, 30)[-1][0][1])
        go(ui, "alt-r")
        self.assertEqual(ui.screen.name, "auth")
        self.assertIn("no usable account", self.text(ui))


class TheJobsScreenAndPane(ShellCase):
    def test_every_read_is_listed_with_its_state_newest_first(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("done") or 0)
        go(ui, "alt-r"); ui.press("alt-j")
        self.assertEqual(ui.screen.name, "jobs")
        text = self.text(ui, 130, 30)
        self.assertIn("Fleet report (lab)", text)
        self.assertIn("(nothing printed yet)", text)
        gate.set(); wait(ui)
        text = self.text(ui, 130, 30)
        self.assertIn("ok Fleet report (lab)", text)
        self.assertIn("done", text)

    def test_a_failed_job_says_so_and_shows_what_it_printed(self):
        ui = self.make(run=lambda args: print("error: Panorama said no") or 2)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-j")
        text = self.text(ui, 130, 30)
        self.assertIn("!! Fleet report (lab)", text)
        self.assertIn("Panorama said no", text)
        self.assertIn("failed", text)

    def test_up_and_down_choose_a_job_and_the_output_follows(self):
        ui = self.make(run=lambda args: print(f"output for {args.command}") or 0)
        go(ui, "alt-r"); wait(ui)
        go(ui, "alt-p"); wait(ui)
        ui.press("alt-j")
        self.assertIn("output for plan", self.text(ui, 130, 30))
        ui.press("down")
        self.assertIn("output for report", self.text(ui, 130, 30))
        self.assertNotIn("output for plan", self.text(ui, 130, 30).split("|", 1)[1])
        ui.press("up")
        self.assertIn("output for plan", self.text(ui, 130, 30))

    def test_a_wide_window_shows_the_jobs_pane_beside_every_other_screen_and_a_narrow_one_does_not(self):
        ui = self.make(run=lambda args: print("x") or 0)
        self.assertNotIn("Background jobs", self.text(ui, 130, 30))      # nothing has run: no pane
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-h")
        wide = self.text(ui, 130, 30)
        self.assertIn("Background jobs", wide)
        self.assertIn("ok Fleet report (lab)", wide)
        self.assertIn("Panorama profile", wide)                           # the screen itself is still there
        self.assertNotIn("Background jobs", self.text(ui, 100, 30))
        for line in ui.render(130, 30):
            self.assertLessEqual(sum(len(t) for _s, t in line), 130)

    def test_a_job_keeps_a_timestamped_log_of_what_it_was_doing(self):
        job = shell.Job(lambda: [progress.say(f"step {i}") for i in range(3)] and print("printed at the end") or 0)
        end = time.monotonic() + 3
        while job.running and time.monotonic() < end:
            time.sleep(0.01)
        log = job.log()
        self.assertRegex(log[0], r"^\d\d:\d\d:\d\d  step 0$")
        self.assertEqual([line[10:] for line in log[:3]], ["step 0", "step 1", "step 2"])
        self.assertEqual(log[-1], "printed at the end")
        self.assertEqual(job.recent(2), ["step 2", "printed at the end"])

    def test_the_pane_shows_the_last_five_lines_of_the_newest_and_of_every_running_job_and_one_line_for_the_rest(self):
        gate = threading.Event()

        def run(args):
            for i in range(12):
                progress.say(f"call number {i}")
            if args.command == "plan":
                gate.wait(3)
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); wait(ui)
        go(ui, "alt-p"); time.sleep(0.2)
        ui.press("alt-h")
        pane = self.text(ui, 140, 40)
        for shown in (7, 8, 9, 10, 11):
            self.assertIn(f"call number {shown}", pane)
        self.assertNotIn("call number 6", pane)                       # five, not more
        self.assertIn("ok Fleet report (lab)", pane)                  # the finished older one: a line, not its log
        self.assertEqual(pane.count("call number 11"), 1)
        gate.set(); wait(ui)

    def test_tab_moves_into_the_log_of_the_chosen_job_and_the_whole_history_scrolls(self):
        ui = self.make(run=lambda args: [progress.say(f"call number {i}") for i in range(80)] and 0)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-j")
        first = self.text(ui, 130, 24)
        self.assertIn("call number 79", first)                         # a log follows its end
        self.assertNotRegex(first, r"call number 0\b")                 # the start is off the top of the screen
        self.assertIn("Tab or Right: into its log", first)
        ui.press("tab")
        self.assertEqual(ui.screen.focus, "log")
        self.assertIn("scroll the log", self.text(ui, 130, 24))
        ui.press("home")
        top = self.text(ui, 130, 24)
        self.assertIn("call number 0", top)
        self.assertNotIn("call number 79", top)
        ui.press("pgdn")
        self.assertIn("call number 1", self.text(ui, 130, 24).replace("call number 10", ""))     # moved on, not stuck
        ui.press("end")
        self.assertIn("call number 79", self.text(ui, 130, 24))
        ui.press("up")
        self.assertIn("call number 78", self.text(ui, 130, 24))
        ui.press("left")
        self.assertEqual(ui.screen.focus, "list")
        ui.press("esc")
        self.assertEqual(ui.screen.name, "home")

    def test_in_the_log_up_and_down_scroll_and_in_the_list_they_choose_a_job(self):
        ui = self.make(run=lambda args: [progress.say(f"{args.command} line {i}") for i in range(60)] and 0)
        go(ui, "alt-r"); wait(ui); go(ui, "alt-p"); wait(ui)
        ui.press("alt-j")
        self.assertIn("plan line 59", self.text(ui, 130, 24))
        ui.press("down")                                               # list focus: the next job
        self.assertIn("report line 59", self.text(ui, 130, 24))
        ui.press("tab"); ui.press("up")                                # log focus: scroll, the job stays
        text = self.text(ui, 130, 24)
        self.assertIn("report line 58", text)
        self.assertNotIn("plan line", text.split("| ", 1)[1])

    def test_a_running_jobs_log_follows_new_lines_until_you_scroll_up(self):
        gate, second = threading.Event(), threading.Event()

        def run(args):
            for i in range(40):
                progress.say(f"early {i}")
            gate.wait(3)
            progress.say("late line")
            second.set()
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); time.sleep(0.2); ui.press("alt-j"); ui.press("tab")
        self.assertIn("early 39", self.text(ui, 130, 24))
        gate.set(); second.wait(3)
        self.assertIn("late line", self.text(ui, 130, 24))             # followed
        ui.press("pgup")
        self.assertNotIn("late line", self.text(ui, 130, 24))          # scrolled away and stays put
        wait(ui)

    def test_the_log_fits_a_narrow_window_with_the_list_above_it(self):
        ui = self.make(run=lambda args: [progress.say(f"call number {i}") for i in range(50)] and 0)
        go(ui, "alt-r"); wait(ui); ui.press("alt-j"); ui.press("tab")
        lines = ui.render(80, 24)
        self.assertEqual(len(lines), 24)
        self.assertIn("call number 49", tui.plain(lines))
        for line in lines:
            self.assertLessEqual(sum(len(t) for _s, t in line), 80)

    def test_the_forms_keep_their_room_and_only_share_a_very_wide_window(self):
        ui = self.make(run=lambda args: print("x") or 0)
        go(ui, "alt-r"); wait(ui)
        ui.press("alt-a")
        self.assertNotIn("Background jobs", self.text(ui, 140, 36))
        self.assertIn("last sign-in test", self.text(ui, 140, 36))           # the whole table is there
        self.assertIn("Background jobs", self.text(ui, 175, 36))
        ui.press("alt-s")
        self.assertNotIn("Background jobs", self.text(ui, 140, 36))

    def test_a_running_job_is_marked_differently_from_the_separator(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("x") or 0)
        go(ui, "alt-r"); ui.press("alt-h")
        self.assertIn(">> Fleet report (lab)", self.text(ui, 130, 30))
        gate.set(); wait(ui)

    def test_a_narrow_window_says_in_the_footer_that_jobs_are_running(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("x") or 0)
        go(ui, "alt-r"); ui.press("alt-h")
        self.assertIn("1 job running", self.text(ui, 100, 30).splitlines()[-1])
        gate.set(); wait(ui)
        self.assertNotIn("job running", self.text(ui, 100, 30).splitlines()[-1])

    def test_the_home_screen_summarises_the_jobs(self):
        ui = self.make()
        self.assertIn("nothing has run yet", self.text(ui))
        go(ui, "alt-r"); wait(ui); ui.press("alt-h")
        self.assertIn("0 running, 1 finished", self.text(ui))

    def test_progress_words_from_the_work_are_shown_beside_the_running_job(self):
        gate = threading.Event()
        started = threading.Event()

        def run(args):
            progress.say("asking Panorama: show devices all")
            started.set()
            gate.wait(3)
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r")
        started.wait(3)
        self.assertIn("asking Panorama: show devices all", self.text(ui))
        ui.press("alt-h")
        self.assertIn("asking Panorama: show devices all", self.text(ui, 130, 30))     # in the pane too
        gate.set(); wait(ui)

    def test_two_jobs_at_once_keep_their_output_apart(self):
        gates = {"report": threading.Event(), "plan": threading.Event()}

        def run(args):
            print(f"start {args.command}")
            gates[args.command].wait(3)
            print(f"end {args.command}")
            return 0
        ui = self.make(run=run)
        go(ui, "alt-r"); go(ui, "alt-p")
        time.sleep(0.1)
        gates["plan"].set(); gates["report"].set()
        wait(ui)
        by_title = {job.title: job.text for job in ui.jobs}
        self.assertEqual(by_title["Fleet report (lab)"], "start report\nend report\n")
        self.assertEqual(by_title["Plan the upgrade (lab)"], "start plan\nend plan\n")

    def test_the_real_streams_are_put_back_once_the_work_is_done(self):
        before = (__import__("sys").stdout, __import__("sys").stderr)
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        self.assertEqual((__import__("sys").stdout, __import__("sys").stderr), before)

    def test_old_finished_jobs_are_dropped_beyond_a_limit_but_running_ones_never_are(self):
        ui = self.make(run=lambda args: 0)
        for _ in range(shell.KEPT_JOBS + 5):
            job = ui.start_job("filler", lambda: 0)
            while job.running:
                time.sleep(0.001)
        gate = threading.Event()
        ui.start_job("slow", lambda: gate.wait(3))
        ui.start_job("another", lambda: 0)
        self.assertLessEqual(len([j for j in ui.jobs if not j.running]), shell.KEPT_JOBS)
        self.assertIn("slow", [j.title for j in ui.jobs])
        gate.set()


class ThePrepareScreen(ShellCase):
    def at_review(self, **kw):
        ui = self.make(**kw)
        go(ui, "alt-5"); wait(ui)
        return ui

    def approve(self, ui, word="APPROVE"):
        ui.press("enter")                                         # the first button: Approve and run...
        for ch in word:
            ui.press(ch)
        ui.press("enter")

    def test_opening_it_reads_the_plan_and_changes_nothing(self):
        ui = self.at_review()
        text = self.text(ui)
        self.assertIn("PREPARE PLAN TEXT", text)
        self.assertIn("2 firewalls to prepare. Nothing has been changed yet.", text)
        self.assertIn("[ Approve and run... ]", text)
        self.assertEqual((self.reviews, self.executed), (["12.1.10"], []))

    def test_it_says_what_is_missing_instead_of_reading(self):
        ui = self.make(target="")
        go(ui, "alt-5")
        self.assertIn("Type the target PAN-OS version", self.text(ui))
        ui2 = self.make(address="")
        go(ui2, "alt-5")
        self.assertIn("type its address", self.text(ui2))
        self.assertEqual(self.reviews, [])

    def test_nothing_runs_until_approve_is_typed_and_anything_else_does_nothing(self):
        ui = self.at_review()
        ui.press("enter")
        text = self.text(ui)
        self.assertIn("approve exactly this", text)
        self.assertIn("fw-one (active) -> fw-two (passive): force the active member's configuration", text)
        self.assertIn("fw-one: download 10.2.3 (does not install it)", text)
        self.assertIn("Type APPROVE to go ahead", text)
        for ch in "yes":
            ui.press(ch)
        ui.press("enter")
        self.assertIn("Nothing was started", self.text(ui))
        self.assertEqual(self.executed, [])
        ui.press("esc")                                           # back to the plan without doing anything
        self.assertIn("[ Approve and run... ]", self.text(ui))
        self.assertEqual(self.executed, [])

    def test_typing_the_word_in_any_case_runs_exactly_the_plan_that_was_read_with_an_approval_bound_to_it(self):
        ui = self.at_review()
        self.approve(ui, "approve")
        wait(ui)
        self.assertEqual(len(self.executed), 1)
        bundle, approval = self.executed[0]
        self.assertEqual((approval.plan_hash, approval.serials), ("ab" * 32, ["S1", "S2"]))
        self.assertIn("typed APPROVE on the prepare screen", approval.approved_by)
        text = self.text(ui)
        self.assertIn("PREPARE RAN", text)
        self.assertIn("finished at", text)
        self.assertEqual(self.reviews, ["12.1.10"])                # it did not read a second plan to run

    def test_what_is_being_approved_wraps_so_none_of_it_is_cut_off(self):
        ui = self.at_review()
        ui.press("enter")
        flat = " ".join(tui.plain(ui.render(70, 30)).split())
        self.assertIn("force the active member's configuration onto the passive, once the content versions match", flat)

    def test_a_plan_that_has_gone_stale_cannot_be_approved(self):
        ui = self.at_review()
        ui.screens["prepare"].bundle.created -= shell.PLAN_MAX_AGE + 60
        ui.press("enter")
        self.assertIn("Read it again before approving", self.text(ui))
        self.assertEqual(ui.screen.stage, "review")
        self.assertEqual(self.executed, [])

    def test_a_plan_that_goes_stale_while_the_approval_is_being_typed_is_refused_at_the_last_moment(self):
        ui = self.at_review()
        ui.press("enter")
        ui.screens["prepare"].bundle.created -= shell.PLAN_MAX_AGE + 60
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        self.assertEqual(self.executed, [])
        self.assertIn("too old to approve", self.text(ui))

    def test_a_different_address_or_target_means_the_old_plan_is_not_the_one_to_approve(self):
        ui = self.at_review()
        ui.press("alt-h")
        ui.session.target = "12.1.11"
        go(ui, "alt-5"); wait(ui)
        self.assertEqual(self.reviews, ["12.1.10", "12.1.11"])
        self.assertEqual(self.executed, [])

    def test_leaving_and_coming_back_keeps_the_plan_and_does_not_read_again(self):
        ui = self.at_review()
        ui.press("alt-h"); go(ui, "alt-5")
        self.assertIn("PREPARE PLAN TEXT", self.text(ui))
        self.assertEqual(self.reviews, ["12.1.10"])

    def test_while_it_runs_the_screen_shows_the_live_output_and_quitting_asks_first(self):
        gate, started = threading.Event(), threading.Event()

        def execute(args, bundle, approve):
            print("syncing fw-one -> fw-two")
            started.set()
            gate.wait(3)
            return 0
        ui = self.at_review(execute=execute)
        self.approve(ui)
        started.wait(3)
        text = self.text(ui)
        self.assertIn("syncing fw-one -> fw-two", text)
        self.assertIn("running...", text)
        self.assertIn("RUNNING", ui.status("prepare")[1])
        self.assertIsNone(ui.press("ctrl-c"))                     # not by accident
        self.assertIn("Prepare is changing firewalls", ui.message)
        self.assertEqual(ui.press("ctrl-c"), "cancel")            # the same key again means it
        ui.press("alt-h")
        ui.screen.at = ui.screen.stops() - 1
        self.assertIsNone(ui.press("enter"))                      # Quit on the home screen asks as well
        self.assertEqual(ui.press("enter"), "accept")
        gate.set(); wait(ui)

    def test_a_key_in_between_disarms_the_quit_question(self):
        gate = threading.Event()
        ui = self.at_review(execute=lambda a, b, c: gate.wait(3) and 0)
        self.approve(ui)
        self.assertIsNone(ui.press("ctrl-c"))
        ui.press("down")
        self.assertIsNone(ui.press("ctrl-c"))                     # asks again
        gate.set(); wait(ui)
        self.assertEqual(ui.press("ctrl-c"), "cancel")            # nothing is changing any more

    def test_leaving_while_it_runs_is_safe_the_job_is_listed_and_it_finishes_on_its_own(self):
        gate = threading.Event()
        ui = self.at_review(execute=lambda a, b, c: gate.wait(3) and print("all done") or 0)
        self.approve(ui)
        ui.press("alt-h")
        self.assertIn("RUNNING", self.text(ui))
        self.assertIn(">> Prepare (lab)", self.text(ui, 130, 30))
        gate.set(); wait(ui)
        go(ui, "alt-5")
        self.assertIn("all done", self.text(ui))

    def test_a_second_run_cannot_be_started_while_one_is_going(self):
        gate = threading.Event()
        ui = self.at_review(execute=lambda a, b, c: gate.wait(3) and 0)
        self.approve(ui)
        ui.press("alt-h"); go(ui, "alt-5")
        self.assertEqual(ui.screen.stage, "running")
        self.assertEqual([label for label, _ in ui.screen.buttons()], ["Stop after the steps in progress", "View: combined output", "Home (it keeps running)"])
        ui.screen.read_plan()
        self.assertEqual(self.reviews, ["12.1.10"])
        gate.set(); wait(ui)

    def test_nothing_to_prepare_goes_straight_on_without_an_approval_to_type(self):
        ui = self.at_review(review=lambda a: print("nothing to prepare") or FakeBundle(serials=()))
        self.assertIn("nothing to prepare", self.text(ui))
        self.assertIn("[ Continue to the upgrade kit ]", self.text(ui))
        ui.press("enter"); wait(ui)
        self.assertEqual(len(self.executed), 1)

    def test_a_rehearsal_is_a_dry_run_through_the_command_and_never_the_real_run(self):
        ui = self.at_review()
        ui.press("right"); ui.press("enter"); wait(ui)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.commands, [("prepare", "12.1.10")])
        self.assertIn("output of prepare", self.text(ui))

    def test_after_a_run_the_report_and_plan_are_out_of_date_and_are_forgotten(self):
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        self.assertIsNotNone(ui.screens["report"].record())
        go(ui, "alt-5"); wait(ui)
        self.approve(ui); wait(ui)
        self.assertIsNone(ui.screens["report"].record())

    def test_a_plan_that_cannot_be_built_shows_why_and_offers_to_try_again(self):
        ui = self.make(review=lambda a: print("error: cannot reach 192.0.2.1") or 2)
        go(ui, "alt-5"); wait(ui)
        text = self.text(ui)
        self.assertIn("cannot reach 192.0.2.1", text)
        self.assertIn("could not be built", text)
        self.assertIn("[ Read the plan again ]", text)
        self.assertEqual(self.executed, [])

    def test_a_sign_in_problem_while_reading_the_plan_goes_to_the_sign_in_screen(self):
        ui = self.make(review=lambda a: setattr(a, "_sign_in_problem", "no usable account") or 2)
        go(ui, "alt-5"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        self.assertIn("no usable account", self.text(ui))

    def test_a_reset_from_elsewhere_cannot_lose_a_run_in_progress(self):
        gate = threading.Event()
        ui = self.at_review(execute=lambda a, b, c: gate.wait(3) and 0)
        self.approve(ui)
        ui.screens["prepare"].reset()
        self.assertEqual(ui.screens["prepare"].stage, "running")
        gate.set(); wait(ui)

    def test_the_home_screen_says_where_it_stands(self):
        ui = self.make()
        self.assertIn("not started", ui.status("prepare")[1])
        go(ui, "alt-5"); wait(ui)
        self.assertIn("2 firewalls to prepare - waiting for your approval", ui.status("prepare")[1])
        self.approve(ui); wait(ui)
        self.assertIn("finished at", ui.status("prepare")[1])

    def test_the_screen_fits_a_small_window_and_keeps_its_buttons(self):
        ui = self.at_review(review=lambda a: print("\n".join(f"line {i}" for i in range(80))) or FakeBundle())
        for height in (12, 24):
            lines = ui.render(80, height)
            self.assertEqual(len(lines), height)
            self.assertIn("[ Approve and run... ]", tui.plain(lines))


class TheUpgradeScreen(ShellCase):
    def at_review(self, **kw):
        ui = self.make(**kw)
        go(ui, "alt-6"); wait(ui)
        return ui

    def approve(self, ui, index=0, word="APPROVE"):
        for _ in range(index):
            ui.press("right")
        ui.press("enter")
        for ch in word:
            ui.press(ch)
        ui.press("enter")

    def test_opening_it_reads_and_shows_the_plan_and_changes_nothing(self):
        ui = self.at_review()
        text = self.text(ui)
        self.assertIn("UPGRADE PLAN TEXT", text)
        self.assertIn("2 ready, 0 need acknowledgement, 0 blocked", text)
        self.assertIn("[ Approve and run... ]", text)
        args = self.up_reviews[0]
        self.assertEqual((args.command, args.target, args.dry_run), ("upgrade", "12.1.10", False))
        self.assertEqual(self.up_executed, [])

    def test_it_says_what_is_missing_instead_of_reading(self):
        ui = self.make(target="")
        go(ui, "alt-6")
        self.assertIn("Type the target PAN-OS version", self.text(ui))
        self.assertEqual(self.up_reviews, [])

    def test_the_approval_lists_every_firewall_the_order_and_every_caution_it_acknowledges(self):
        ui = self.at_review()
        ui.press("enter")
        text = self.text(ui, 120, 40)
        for needle in ("approve exactly this", "LIVE: this changes real firewalls", "fw-one (A1) active  10.2.1 -> 10.2.3", "fw-two (A2) passive",
                       "1 HA failover(s)", "1. fw-two then fw-one", "units run at the same time", "Cautions that approving acknowledges", "[failover_band] fw-one: sessions may move",
                       "Type APPROVE to go ahead"):
            self.assertIn(needle, text)
        self.assertEqual(self.up_executed, [])

    def test_nothing_runs_until_the_word_is_typed(self):
        ui = self.at_review()
        ui.press("enter")
        for ch in "go":
            ui.press(ch)
        ui.press("enter")
        self.assertIn("Nothing was started", self.text(ui))
        ui.press("esc")
        self.assertEqual(self.up_executed, [])
        self.assertIn("[ Approve and run... ]", self.text(ui))

    def test_the_word_runs_exactly_the_plan_that_was_read_with_an_approval_naming_its_firewalls_and_cautions(self):
        ui = self.at_review()
        self.approve(ui, word="approve")
        wait(ui)
        [(args, bundle, approval)] = self.up_executed
        self.assertEqual((approval.plan_hash, approval.serials, approval.acks), ("cd" * 32, ["A2", "A1"], ["A1:failover_band"]))
        self.assertIn("typed APPROVE on the upgrade screen", approval.approved_by)
        self.assertEqual((args.resume, args.fresh, args.dry_run), (False, False, False))
        self.assertIn("UPGRADE RAN", self.text(ui))
        self.assertEqual(len(self.up_reviews), 1)

    def test_a_stale_plan_cannot_be_approved_or_run(self):
        ui = self.at_review()
        ui.screens["upgrade"].bundle.created -= shell.PLAN_MAX_AGE + 60
        ui.press("enter")
        self.assertIn("Read it again before approving", self.text(ui))
        ui2 = self.at_review()
        ui2.press("enter")
        ui2.screens["upgrade"].bundle.created -= shell.PLAN_MAX_AGE + 60
        for ch in "APPROVE":
            ui2.press(ch)
        ui2.press("enter")
        self.assertIn("too old to approve", self.text(ui2))
        self.assertEqual(self.up_executed, [])

    def test_an_unfinished_earlier_run_must_be_chosen_between_and_the_choice_is_passed_on(self):
        previous = ("C:/runs/upgrade-1.jsonl", "3 steps done, 0 failed, 1 started but not finished, 5 not reached")
        ui = self.at_review(up_review=lambda a: print("plan") or FakeUpgrade(previous=previous))
        text = self.text(ui, 140, 30)
        self.assertIn("[ Approve and CONTINUE the earlier run... ]", text)
        self.assertIn("[ Approve and start a NEW run... ]", text)
        self.assertNotIn("[ Approve and run... ]", text)
        self.approve(ui, index=0); wait(ui)
        args = self.up_executed[-1][0]
        self.assertEqual((args.resume, args.fresh), (True, False))
        ui = self.at_review(up_review=lambda a: print("plan") or FakeUpgrade(previous=previous))
        ui.press("enter"); self.assertIn("Continues the earlier run", self.text(ui, 140, 40)); ui.press("esc")
        self.approve(ui, index=1); wait(ui)
        args = self.up_executed[-1][0]
        self.assertEqual((args.resume, args.fresh), (False, True))

    def test_the_screen_never_leaves_the_choice_of_journal_to_a_prompt(self):
        ui = self.at_review()
        self.approve(ui); wait(ui)
        [(args, _, _)] = self.up_executed
        self.assertEqual((args.resume, args.fresh), (False, False))      # no earlier run: nothing to choose, and cli passes tty=False if there were

    def test_nothing_ready_offers_no_approval_at_all(self):
        ui = self.at_review(up_review=lambda a: print("plan") or FakeUpgrade(ready=(), blocked=("A1",)))
        text = self.text(ui, 140, 30)
        self.assertIn("Nothing is ready to upgrade", text)
        self.assertNotIn("Approve", text)
        self.assertIn("[ Standalone firewalls: left out ]", text)
        self.assertIn("nothing is ready (1 blocked)", ui.status("upgrade")[1])

    def test_standalone_firewalls_are_left_out_until_asked_for_and_then_the_plan_is_read_again(self):
        ui = self.at_review()
        ui.press("right"); ui.press("right"); ui.press("enter"); wait(ui)       # Approve, Rehearse, then the standalone switch
        self.assertEqual(len(self.up_reviews), 2)
        self.assertIn("--accept-standalone-outage", ["--accept-standalone-outage"] if self.up_reviews[1].accept_standalone_outage else [])
        self.assertFalse(self.up_reviews[0].accept_standalone_outage)
        text = self.text(ui, 160, 30)
        self.assertIn("Standalone firewalls: included (each is OFFLINE while it reboots)", text)

    def test_stop_asks_the_run_to_start_nothing_new_and_it_ends_by_itself(self):
        started = threading.Event()

        def execute(args, bundle, approve):
            started.set()
            bundle.stop.wait(3)
            print("stopped early" if bundle.stop.is_set() else "ran out of time")
            return 1
        ui = self.at_review(up_execute=execute)
        self.approve(ui)
        started.wait(3)
        self.assertEqual(ui.screen.buttons()[0][0], "Stop after the steps in progress")
        ui.press("enter")
        self.assertTrue(self.up_executed[-1][1].stop.is_set())
        self.assertIn("Stopping: steps already going will finish", self.text(ui))
        wait(ui)
        text = self.text(ui)
        self.assertIn("stopped early", text)
        self.assertIn("with an error (code 1)", text)

    def test_quitting_while_it_runs_asks_first_and_names_the_upgrade(self):
        gate = threading.Event()
        ui = self.at_review(up_execute=lambda a, b, c: gate.wait(3) and 0)
        self.approve(ui)
        self.assertIsNone(ui.press("ctrl-c"))
        self.assertIn("Upgrade is changing firewalls", ui.message)
        self.assertEqual(ui.press("ctrl-c"), "cancel")
        self.assertIn("RUNNING - upgrading firewalls now", ui.status("upgrade")[1])
        gate.set(); wait(ui)

    def test_a_rehearsal_is_a_dry_run_through_the_command_and_never_the_real_run(self):
        ui = self.at_review()
        ui.press("right"); ui.press("enter"); wait(ui)
        self.assertEqual(self.up_executed, [])
        self.assertEqual(self.commands, [("upgrade", "12.1.10")])

    def test_after_a_run_what_was_read_about_the_fleet_is_forgotten(self):
        ui = self.make()
        go(ui, "alt-r"); wait(ui)
        go(ui, "alt-5"); wait(ui)
        self.assertIsNotNone(ui.screens["report"].record())
        go(ui, "alt-6"); wait(ui)
        self.approve(ui); wait(ui)
        self.assertIsNone(ui.screens["report"].record())
        self.assertEqual(ui.screens["prepare"].stage, "idle")

    def test_a_plan_that_cannot_be_built_shows_why(self):
        ui = self.make(up_review=lambda a: print("error: cannot reach 192.0.2.1") or 2)
        go(ui, "alt-6"); wait(ui)
        self.assertIn("cannot reach 192.0.2.1", self.text(ui))
        self.assertEqual(self.up_executed, [])

    def test_the_screen_fits_a_small_window_and_keeps_its_buttons(self):
        ui = self.at_review(up_review=lambda a: print("\n".join(f"line {i}" for i in range(90))) or FakeUpgrade())
        for height in (12, 24):
            lines = ui.render(80, height)
            self.assertEqual(len(lines), height)
            self.assertIn("Approve and run", tui.plain(lines))


class ChoosingTheTargetFromAFirewall(ShellCase):
    """The home screen's text field takes any version typed; the picker lists what a firewall itself offers."""

    OFFERED = [{"version": "10.2.4-h2", "downloaded": True, "current": False, "latest": True, "released": "2026/09/01"},
               {"version": "10.2.3", "downloaded": False, "current": True, "latest": False, "released": "2026/07/01"}]

    def make_picker(self, versions=None, error=None):
        asked = []

        def read_versions(args, serial):
            asked.append(serial)
            if error:
                raise SourceError(error)
            return self.OFFERED if versions is None else versions
        ui = self.make(read_versions=read_versions)
        ui.press("down"); ui.press("down"); ui.press("down")          # the row under the target field
        ui.press("enter"); wait(ui)
        return ui, asked

    def test_the_home_screen_offers_it_under_the_target_field_and_typing_still_works(self):
        ui = self.make()
        self.assertIn("Or choose it from the versions a firewall lists", self.text(ui))
        ui.press("down"); ui.press("down")
        for ch in "9.9.9":
            ui.press(ch)
        self.assertEqual(ui.session.target, "12.1.10" + "9.9.9")

    def test_pick_a_firewall_then_narrow_the_versions_tier_by_tier_and_the_target_becomes_exactly_that_text(self):
        ui, asked = self.make_picker()
        self.assertEqual(ui.screen.name, "targets")
        text = self.text(ui)
        for ref in REFS:
            if ref.connected:
                self.assertIn(ref.hostname, text)
        ui.press("enter"); wait(ui)
        self.assertEqual(len(asked), 1)
        text = self.text(ui)
        self.assertIn("Major release", text)
        self.assertIn("2 versions", text)
        ui.press("enter")                                                  # 10
        self.assertIn("Feature release", self.text(ui))
        ui.press("enter")                                                  # 10.2
        text = self.text(ui)
        self.assertIn("Maintenance release", text)
        self.assertIn("10.2.4", text)
        self.assertIn("10.2.3", text)
        self.assertIn("running", text)
        ui.press("enter")                                                  # 10.2.4: it has a hotfix, so the hotfix tier is next
        text = self.text(ui)
        self.assertIn("Hotfix", text)
        self.assertIn("10.2.4-h2", text)
        self.assertIn("downloaded, latest", text)
        ui.press("enter")
        self.assertEqual(ui.session.target, "10.2.4-h2")
        self.assertEqual(ui.screen.name, "home")
        self.assertIn("Target set to 10.2.4-h2", self.text(ui))

    def test_a_maintenance_release_without_a_hotfix_is_chosen_straight_away(self):
        ui, asked = self.make_picker()
        ui.press("enter"); wait(ui)
        ui.press("enter"); ui.press("enter")
        ui.press("down")                                                   # 10.2.3, which has no hotfix
        ui.press("enter")
        self.assertEqual((ui.session.target, ui.screen.name), ("10.2.3", "home"))

    def test_while_the_firewall_is_asked_the_screen_says_so_and_offers_nothing_to_pick(self):
        gate, begun = threading.Event(), threading.Event()

        def read_versions(args, serial):
            begun.set()
            gate.wait(3)
            return self.OFFERED
        ui = self.make(read_versions=read_versions)
        ui.press("down"); ui.press("down"); ui.press("down")
        ui.press("enter"); wait(ui)
        ui.press("down"); ui.press("enter")                                # the second firewall
        begun.wait(3)
        text = self.text(ui)
        self.assertIn("Getting the available PAN-OS versions from " + REFS[1].hostname, text)
        self.assertNotIn(REFS[0].hostname + " ", text.replace("Getting", ""))   # the list of firewalls is not shown to be picked from
        ui.press("enter")                                                  # nothing happens: no second question
        self.assertEqual(len([j for j in ui.jobs if j.title.startswith("Versions offered")]), 1)
        gate.set(); wait(ui)
        self.assertIn("Major release", self.text(ui))

    def test_escape_while_it_is_asked_cancels_and_returns_to_the_firewalls(self):
        gate, begun = threading.Event(), threading.Event()

        def read_versions(args, serial):
            begun.set()
            gate.wait(3)
            return self.OFFERED
        ui = self.make(read_versions=read_versions)
        ui.press("down"); ui.press("down"); ui.press("down")
        ui.press("enter"); wait(ui)
        ui.press("enter")
        begun.wait(3)
        ui.press("esc")
        self.assertEqual(ui.screen.name, "targets")
        self.assertIn("Pick a firewall", self.text(ui))
        gate.set(); wait(ui)
        self.assertIn("Pick a firewall", self.text(ui))                    # the late answer does not pull the person into a list

    def test_escape_goes_back_one_tier_at_a_time_then_to_the_firewalls_then_home_without_changing_the_target(self):
        ui, asked = self.make_picker()
        ui.press("enter"); wait(ui)
        ui.press("enter"); ui.press("enter")                               # 10, 10.2
        ui.press("esc")
        self.assertIn("Feature release", self.text(ui))
        ui.press("esc")
        self.assertIn("Major release", self.text(ui))
        ui.press("esc")
        self.assertEqual(ui.screen.name, "targets")
        self.assertIn("Pick a firewall", self.text(ui))
        ui.press("esc")
        self.assertEqual(ui.screen.name, "home")
        self.assertEqual(ui.session.target, "12.1.10")

    def test_a_long_list_is_cut_into_short_ones(self):
        many = [{"version": f"{maj}.{minor}.{m}" + (f"-h{h}" if h else ""), "downloaded": False, "current": False, "latest": False, "released": ""}
                for maj, minor in ((10, 1), (10, 2), (11, 0), (11, 1), (12, 1)) for m in range(0, 12) for h in range(0, 4)]
        ui, asked = self.make_picker(versions=many)
        ui.press("enter"); wait(ui)
        text = self.text(ui, 100, 30)
        self.assertIn("96 versions", text)                                 # 240 versions, shown as the three majors
        rows = [line for line in text.splitlines() if "versions" in line and line.strip().startswith((">", "10", "11", "12"))]
        self.assertEqual(len(rows), 3)

    def test_a_firewall_that_cannot_say_leaves_a_reason_and_the_list_of_firewalls(self):
        ui, asked = self.make_picker(error="no route to the update server")
        ui.press("enter"); wait(ui)
        text = self.text(ui)
        self.assertIn("no route to the update server", text)
        self.assertIn(REFS[0].hostname, text)

    def test_a_firewall_that_lists_nothing_says_so(self):
        ui, asked = self.make_picker(versions=[])
        ui.press("enter"); wait(ui)
        self.assertIn("lists no versions", self.text(ui))


class TheSoftwareListAFirewallOffers(unittest.TestCase):
    def test_the_list_is_read_newest_first_with_what_the_firewall_says_about_each(self):
        from panupgrade import xmlops
        asked = []
        xml = ("<response status=\"success\"><result><sw-updates><versions>"
               "<entry><version>10.2.3</version><downloaded>yes</downloaded><current>yes</current><latest>no</latest><released-on>2026/07/01</released-on></entry>"
               "<entry><version>10.2.10</version><downloaded>no</downloaded><current>no</current><latest>yes</latest></entry>"
               "<entry><version>10.2.4-h2</version><downloaded>no</downloaded><current>no</current><latest>no</latest></entry>"
               "</versions></sw-updates></result></response>")

        def op(command):
            asked.append(command)
            import xml.etree.ElementTree as ET
            return ET.fromstring(xml)
        rows = xmlops.parse_software_versions(op)
        self.assertEqual([r["version"] for r in rows], ["10.2.10", "10.2.4-h2", "10.2.3"])
        self.assertEqual(asked[0], "request system software check")        # it asks the update server first
        self.assertTrue(rows[2]["downloaded"] and rows[2]["current"] and rows[0]["latest"])
        self.assertEqual(rows[2]["released"], "2026/07/01")

    def test_a_firewall_that_cannot_reach_the_update_server_still_gives_the_list_it_has(self):
        from panupgrade import xmlops
        import xml.etree.ElementTree as ET

        def op(command):
            if "check" in command:
                raise xmlops.XmlOpError("no route")
            return ET.fromstring("<response status=\"success\"><result><sw-updates><versions><entry><version>10.2.3</version></entry>"
                                 "</versions></sw-updates></result></response>")
        self.assertEqual([r["version"] for r in xmlops.parse_software_versions(op)], ["10.2.3"])


class FindingsAreData(unittest.TestCase):
    """What a run reports as a problem or a caution reaches the screen as data, from the place that decided it, not from the printed text."""

    def test_a_fail_or_warn_line_is_reported_once_as_data_and_other_lines_are_not(self):
        from panupgrade import console
        heard = []
        progress.listen(None, None, lambda kind, text: heard.append((kind, text)))
        try:
            style = console.Style()
            style.line("fail", "fw-one: BLOCKED - 3584 MB free")
            style.line("warn", "the batch may be short")
            style.line("ok", "fine")
            style.line("start", "download")
        finally:
            progress.listen(None)
        self.assertEqual(heard, [("fail", "fw-one: BLOCKED - 3584 MB free"), ("warn", "the batch may be short")])

    def test_a_job_keeps_what_its_thread_reported_and_a_second_job_does_not_see_it(self):
        from panupgrade import console

        def work():
            console.Style().line("fail", "from the first job")
            return 0
        first = shell.Job(work, "one")
        while first.running:
            time.sleep(0.01)
        second = shell.Job(lambda: 0, "two")
        while second.running:
            time.sleep(0.01)
        self.assertEqual(first.findings, [("fail", "from the first job")])
        self.assertEqual(second.findings, [])

    def test_printed_text_that_only_looks_like_a_finding_is_not_one(self):
        job = shell.Job(lambda: print("[FAIL] this is only text") or 0, "text")
        while job.running:
            time.sleep(0.01)
        self.assertEqual(job.findings, [])


class ThePrepareRunFirewallByFirewall(ShellCase):
    """Prepare gets the same choice as the upgrade: the combined output, or each pair's (or standalone firewall's) own log."""

    def run_with(self, script, serials=("S1", "S2")):
        gate, begun = threading.Event(), threading.Event()
        holder = {}

        def review(args):
            holder["bundle"] = FakeBundle(serials)
            holder["bundle"].plan["downloads"].append({"serial": "S3", "hostname": "fw-three", "version": "10.2.3"})
            holder["bundle"].plan["serials"].append("S3")
            holder["bundle"].groups = prepare_mod.work_groups(holder["bundle"].plan)
            print("plan")
            return holder["bundle"]

        def execute(args, bundle, approve):
            script(bundle)
            begun.set()
            gate.wait(3)
            return 0
        ui = self.make(review=review, execute=execute)
        go(ui, "alt-5"); wait(ui)
        ui.press("enter")
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        begun.wait(3)
        return ui, holder["bundle"], gate

    def test_a_pair_is_one_unit_and_a_standalone_firewall_another(self):
        def script(bundle):
            bundle.step("S1", "backup before sync", "started")
            bundle.step("S3", "download 10.2.3", "started")
        ui, bundle, gate = self.run_with(script)
        text = self.text(ui, 130, 30)
        self.assertIn("fw-two + fw-one", text)
        self.assertIn("fw-three", text)
        self.assertIn("View: combined output", text)
        gate.set(); wait(ui)

    def test_each_units_log_holds_only_its_own_events_and_v_switches_to_the_combined_output(self):
        def script(bundle):
            bundle.step("S2", "backup before sync", "started"); bundle.step("S2", "backup before sync", "ok")
            bundle.step("S3", "download 10.2.3", "started")
        ui, bundle, gate = self.run_with(script)
        right = "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 30).splitlines() if "| " in line and "Background jobs" not in line)
        self.assertRegex(right, r"\d\d:\d\d:\d\d  fw-two\s+backup before sync  started")
        self.assertNotIn("fw-three", right)
        ui.press("down")
        right = "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 30).splitlines() if "| " in line)
        self.assertIn("fw-three       download 10.2.3  started", right)
        ui.press("v")
        self.assertIn("View: by firewall", self.text(ui, 130, 30))
        gate.set(); wait(ui)

    def test_a_unit_is_done_when_everything_planned_for_it_is_done_and_failed_when_a_task_failed(self):
        def script(bundle):
            bundle.step("S1", "sync to peer", "started"); bundle.step("S1", "sync to peer", "ok")
            bundle.step("S1", "download 10.2.3", "started"); bundle.step("S1", "download 10.2.3", "ok")
            bundle.step("S3", "download 10.2.3", "started"); bundle.step("S3", "download 10.2.3", "failed", error="no space")
        ui, bundle, gate = self.run_with(script)
        text = self.text(ui, 130, 30)
        self.assertIn("ok fw-two + fw-one", text)
        self.assertIn("!! fw-three", text)
        self.assertIn("FAILED: no space", text)
        gate.set(); wait(ui)

    def test_a_problem_that_belongs_to_no_one_firewall_is_not_hidden_behind_all_steps_done(self):
        """Every firewall's own steps went well, yet the checks after them found the fleet blocked: the list must say so."""
        def script(bundle):
            bundle.step("S3", "download 10.2.3", "started"); bundle.step("S3", "download 10.2.3", "ok")
            bundle.step("S1", "sync to peer", "started"); bundle.step("S1", "sync to peer", "ok")
            bundle.step("S1", "download 10.2.3", "started"); bundle.step("S1", "download 10.2.3", "ok")
            progress.finding("fail", "fw-three: BLOCKED - 3584 MB free, setting min_free_disk_mb is 4096")
            progress.finding("warn", "The upgrade batch may not include firewalls that failed to prepare")
        ui, bundle, gate = self.run_with(script)
        gate.set(); wait(ui)
        text = self.text(ui, 130, 30)
        self.assertIn("!! Whole fleet", text)
        self.assertIn("1 problem reported, 1 warning(s)", text)
        self.assertIn("ok fw-two + fw-one", text)                                  # the firewalls' own steps did succeed
        self.assertIn("[FAIL] fw-three: BLOCKED - 3584 MB free", text)             # and the fleet's log (shown first) says what is wrong
        self.assertIn("Everything it printed, in order:", text)

    def test_the_stage_the_run_is_in_shows_beside_the_whole_fleet_while_the_firewalls_are_done(self):
        gate, begun = threading.Event(), threading.Event()

        def execute(args, bundle, approve):
            bundle.step("S1", "sync to peer", "started"); bundle.step("S1", "sync to peer", "ok")
            bundle.step("S1", "download 10.2.3", "started"); bundle.step("S1", "download 10.2.3", "ok")
            progress.phase("Post-preparation verifications: reading the fleet again")
            begun.set(); gate.wait(3)
            return 0
        ui = self.make(execute=execute)
        go(ui, "alt-5"); wait(ui)
        ui.press("enter")
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        begun.wait(3)
        text = self.text(ui, 130, 30)
        self.assertIn(">> Whole fleet", text)
        self.assertIn("Post-preparation verifications: reading the fleet again", text)
        gate.set(); wait(ui)

    def test_the_logs_can_still_be_read_after_the_run(self):
        def script(bundle):
            bundle.step("S3", "download 10.2.3", "started"); bundle.step("S3", "download 10.2.3", "ok")
        ui, bundle, gate = self.run_with(script)
        gate.set(); wait(ui)
        ui.press("down"); ui.press("down")                             # nothing is running now: past the whole fleet, to the unit to read
        text = self.text(ui, 130, 30)
        self.assertIn("finished at", text)
        self.assertIn("fw-three       download 10.2.3  ok", text)
        self.assertIn("fw-two + fw-one", text)                          # the pair that never got going says so


class TheRunFirewallByFirewall(ShellCase):
    """While an upgrade runs, each firewall (an HA pair is one unit, passive first) has its own log beside the list of units."""

    def run_with(self, script, ready=("A2", "A1", "B1")):
        """An upgrade whose steps are written by ``script(bundle, gate)``; the test releases ``gate`` to let it finish."""
        gate, begun = threading.Event(), threading.Event()
        holder = {}

        def review(args):
            holder["bundle"] = FakeUpgrade(ready=ready)
            print("plan")
            return holder["bundle"]

        def execute(args, bundle, approve):
            script(bundle, gate, begun)
            gate.wait(3)
            return 0
        ui = self.make(up_review=review, up_execute=execute)
        ui.press("alt-6"); ui.press("enter"); wait(ui)
        ui.press("enter")
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        begun.wait(3)
        return ui, holder["bundle"], gate

    def test_each_unit_is_listed_with_where_it_is_and_the_pair_is_one_unit_named_passive_first(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "preflight", "started"); bundle.step("A2", "preflight", "ok", version="10.2.1")
            bundle.step("A2", "0:backup", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        text = self.text(ui, 130, 30)
        self.assertIn(">> fw-two + fw-one", text)
        self.assertIn("fw-two: backup", text)                             # what the running unit is on
        self.assertIn(".. fw-three", text)
        self.assertIn("waiting its turn", text)
        self.assertIn("View: combined output", text)
        gate.set(); wait(ui)

    def test_the_log_beside_the_list_is_the_chosen_units_events_with_the_time_and_the_firewall(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "0:backup", "started"); bundle.step("A2", "0:backup", "ok")
            bundle.step("A2", "1:install 10.2.3", "started"); bundle.step("A2", "1:install 10.2.3", "ok", version="10.2.3")
            bundle.step("A1", "0:backup", "started")
            bundle.step("B1", "0:backup", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        text = self.text(ui, 130, 30)
        right = "\n".join(line.split("| ", 1)[1] for line in text.splitlines() if "| " in line and "Background jobs" not in line)
        self.assertRegex(right, r"\d\d:\d\d:\d\d  fw-two\s+backup  started")
        self.assertIn("fw-two         install 10.2.3  ok  version=10.2.3", right)
        self.assertIn("fw-one         backup  started", right)         # the active member of the same unit
        self.assertNotIn("fw-three", right)                            # another unit's events are not mixed in
        ui.press("down")                                               # choose the next unit
        right = "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 30).splitlines() if "| " in line)
        self.assertIn("fw-three       backup  started", right)
        self.assertNotIn("fw-two", right)
        gate.set(); wait(ui)

    def test_the_view_follows_the_unit_that_is_running_until_you_choose_one(self):
        def script(bundle, gate, begun):
            for serial in ("A2", "A1"):
                for i, action in enumerate(("backup", "install 10.2.3")):
                    bundle.step(serial, f"{i}:{action}", "started"); bundle.step(serial, f"{i}:{action}", "ok")
            bundle.step("B1", "0:backup", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        right = "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 30).splitlines() if "| " in line)
        self.assertIn("fw-three", right)                               # the first unit is done: the running one is shown
        self.assertIn("ok fw-two + fw-one", self.text(ui, 130, 30))
        gate.set(); wait(ui)

    def test_a_long_step_name_is_cut_at_the_list_edge_and_never_runs_into_the_log(self):
        def script(bundle, gate, begun):
            bundle.step("A1", "0:wait for the passive firewall's upgrade to finish", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script, ready=("A2", "A1"))
        for line in self.text(ui, 124, 24).splitlines():
            if "| " in line and "fw-one" in line and "wait for the passive" in line.split("| ", 1)[0]:
                self.assertEqual(line.index("|"), 40)
        gate.set(); wait(ui)

    def test_a_failure_marks_its_unit_and_shows_the_reason_in_its_log(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "0:backup", "started"); bundle.step("A2", "0:backup", "failed", error="backup was not verified")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        text = self.text(ui, 130, 30)
        self.assertIn("!! fw-two + fw-one", text)
        self.assertIn("fw-two: backup", text)
        self.assertIn("FAILED: backup was not verified", text)
        gate.set(); wait(ui)

    def test_after_the_run_ends_the_logs_can_still_be_read(self):
        def script(bundle, gate, begun):
            for serial in ("A2", "A1"):
                for i, action in enumerate(("backup", "install 10.2.3")):
                    bundle.step(serial, f"{i}:{action}", "started"); bundle.step(serial, f"{i}:{action}", "ok")
            begun.set()
        ui, bundle, gate = self.run_with(script, ready=("A2", "A1"))
        gate.set(); wait(ui)
        text = self.text(ui, 130, 30)
        self.assertIn("finished at", text)
        self.assertIn("ok fw-two + fw-one", text)
        self.assertIn("all steps done", text)
        self.assertIn("fw-one         install 10.2.3  ok", text)

    def test_a_unit_that_never_started_says_so_once_the_run_is_over(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "0:backup", "started"); bundle.step("A2", "0:backup", "ok")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        gate.set(); wait(ui)
        text = self.text(ui, 130, 30)
        self.assertIn("stopped before it finished", text)
        self.assertIn("not started", text)

    def test_up_and_down_choose_a_unit_and_stay_there_and_v_switches_to_the_plain_output(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "0:backup", "started")
            bundle.step("B1", "0:backup", "started")
            print("combined text line")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        ui.press("down")
        bundle.step("A2", "1:install 10.2.3", "started")                # the running unit changes under the chosen one
        right = "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 30).splitlines() if "| " in line)
        self.assertIn("fw-three", right)                                # the chosen unit stays chosen
        self.assertEqual(ui.screen.unit_at, 2)                           # the whole-fleet entry is the first of the list
        ui.press("v")
        text = self.text(ui, 130, 30)
        self.assertIn("combined text line", text)
        self.assertIn("View: by firewall", text)
        ui.press("v")
        self.assertIn("fw-three", self.text(ui, 130, 30))
        gate.set(); wait(ui)

    def test_a_long_log_scrolls_with_page_keys(self):
        def script(bundle, gate, begun):
            for i in range(60):
                bundle.step("A2", f"0:call {i}", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script, ready=("A2", "A1"))
        def log_side():
            return "\n".join(line.split("| ", 1)[1] for line in self.text(ui, 130, 24).splitlines() if "| " in line)
        self.assertIn("call 59  started", log_side())
        ui.press("home")
        self.assertIn("call 0 ", log_side())
        self.assertNotIn("call 59  started", log_side())                          # (the list on the left names the step it is on, which is 59)
        ui.press("end")
        self.assertIn("call 59  started", log_side())
        gate.set(); wait(ui)

    def test_a_narrow_window_puts_the_list_above_the_log_and_fits(self):
        def script(bundle, gate, begun):
            bundle.step("A2", "0:backup", "started")
            begun.set()
        ui, bundle, gate = self.run_with(script)
        lines = ui.render(80, 24)
        self.assertEqual(len(lines), 24)
        for line in lines:
            self.assertLessEqual(sum(len(t) for _s, t in line), 80)
        self.assertIn("fw-two: backup", tui.plain(lines))
        gate.set(); wait(ui)

    def test_a_rehearsal_has_no_per_firewall_log_to_offer(self):
        ui = self.make()
        ui.press("alt-6"); ui.press("enter"); wait(ui)
        ui.press("right"); ui.press("enter"); wait(ui)
        self.assertNotIn("View: by firewall", self.text(ui, 130, 30))
        self.assertNotIn("View: combined output", self.text(ui, 130, 30))


class StagesInPlainWords(ShellCase):
    """The headline beside the spinner names the stage ("Reading firewall 3 of 6: e2"), not the command behind it."""

    def slow(self, review=False):
        gate, shown = threading.Event(), threading.Event()

        def work(args, *rest):
            progress.say("asking firewall SIM0001: show session info")
            progress.phase("Reading firewall 1 of 2: fw-east-1")
            progress.say("asking firewall SIM0001: show system info")
            shown.set()
            gate.wait(3)
            return FakeBundle() if review else 0
        return gate, shown, work

    def test_the_report_headline_is_the_stage_and_the_raw_calls_stay_in_the_jobs_log(self):
        gate, shown, work = self.slow()
        ui = self.make(run=work)
        ui.press("alt-r"); ui.press("enter"); shown.wait(3)
        text = self.text(ui)
        self.assertIn("Reading firewall 1 of 2: fw-east-1", text)
        self.assertNotIn("show session info", text)
        self.assertNotIn("show system info", text)
        ui.press("alt-j")
        self.assertIn("show session info", self.text(ui, 130, 24))      # the detail is one Tab away
        gate.set(); wait(ui)

    def test_reading_the_plan_on_prepare_and_upgrade_says_the_same_in_the_same_words(self):
        for key, make in (("alt-5", "review"), ("alt-6", "up_review")):
            gate, shown, work = self.slow(review=True)
            ui = self.make(**{make: work})
            ui.press(key); ui.press("enter"); shown.wait(3)
            text = self.text(ui)
            self.assertIn("Reading firewall 1 of 2: fw-east-1", text)
            self.assertNotIn("show session info", text)
            gate.set(); wait(ui)

    def test_the_pane_shows_the_stages_not_every_call_while_it_runs(self):
        gate, shown, work = self.slow()
        ui = self.make(run=work)
        ui.press("alt-r"); ui.press("enter"); shown.wait(3); ui.press("alt-h")
        pane = self.text(ui, 140, 30)
        self.assertIn("Reading firewall 1 of 2: fw-east-1", pane)
        self.assertNotIn("show session info", pane)
        gate.set(); wait(ui)

    def test_abort_is_noticed_at_the_next_stage_too(self):
        go, ready = threading.Event(), threading.Event()

        def work(args):
            ready.set()
            go.wait(3)
            progress.phase("Reading firewall 2 of 2: fw-east-2")        # never reached: the person pressed Abort first
            return 0
        ui = self.make(run=work)
        ui.press("alt-r"); ui.press("enter"); ready.wait(3)
        ui.press("enter")                                                # Abort
        go.set(); wait(ui)
        self.assertTrue(ui.jobs[0].aborted)


class WhatTheWorkerThreadsPrint(unittest.TestCase):
    """The run's events are printed by the executor's own threads, not the job's; they must still reach the job's screen."""

    def test_the_event_printer_of_a_job_writes_into_the_jobs_capture_from_any_thread(self):
        names = {"S1": "fw-one"}

        def work():
            printer, close = cli._listener(cli._style(cli.parse_args(["settings"])), names, ["S1"], settings.load(None, ["max_parallel=1"]))
            thread = threading.Thread(target=lambda: printer({"type": "step", "serial": "S1", "step": "0:backup", "state": "started", "detail": {}}))
            thread.start()
            thread.join()
            close()
            return 0
        job = shell.Job(work)
        end = time.monotonic() + 3
        while job.running and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIn("fw-one", job.text)
        self.assertIn("backup", job.text)

    def test_outside_a_job_the_printer_still_uses_the_real_stdout(self):
        self.assertIs(progress.output(), __import__("sys").stdout)


class TheUpgradeCommandPhases(Home):
    """The same code the screen runs, against the simulator: review, then execute with a typed approval, never reading the keyboard."""

    def setUp(self):
        super().setUp()
        from tests.support import fleet_data, pair
        self.fleet = os.path.join(self.tmp.name, "fleet.json")
        with open(self.fleet, "w", encoding="utf-8") as handle:
            json.dump(fleet_data(pair("A1", "A2")), handle)
        self.runs = os.path.join(self.tmp.name, "runs")

    def args(self, *extra):
        return cli.parse_args(["upgrade", "--source", f"fake:{self.fleet}", "--target", "20.2.9", "--runs-dir", self.runs,
                               "--color", "never", "--ascii", *extra])

    def test_the_real_run_hands_every_journal_event_to_the_bundle_for_the_screen(self):
        args = self.args()
        style = cli._style(args)
        with redirect_stdout(io.StringIO()):
            bundle = cli._upgrade_review(args, style)
            cli._upgrade_execute(args, style, bundle, lambda jp: cli._new_approval(
                bundle.plan["hash"], [d["serial"] for d in bundle.ready()], bundle.acks(), jp, "test"))
        events = [rec for _when, rec in bundle.events_snapshot()]
        self.assertEqual({rec["serial"] for rec in events}, {"A1", "A2"})
        names = {(rec["serial"], rec["step"].split(":", 1)[-1]) for rec in events if rec["state"] == "ok"}
        self.assertIn(("A1", "wait for the passive firewall's upgrade to finish"), names)
        first = {rec["serial"]: i for i, rec in reversed(list(enumerate(events)))}
        self.assertLess(first["A2"], first["A1"])                                 # the passive member's events come first

    def test_review_prints_the_plan_changes_nothing_and_finds_no_earlier_run(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            bundle = cli._upgrade_review(self.args(), cli._style(self.args()), show=True)
        self.assertIn("REPORT ONLY", buffer.getvalue())
        self.assertEqual((len(bundle.ready()), bundle.previous, bundle.label), (2, None, "SIMULATION"))
        self.assertFalse(os.path.exists(self.runs))

    def test_a_typed_approval_runs_the_plan_and_is_recorded_next_to_the_journal(self):
        args = self.args()
        style = cli._style(args)
        with redirect_stdout(io.StringIO()):
            bundle = cli._upgrade_review(args, style)
            code = cli._upgrade_execute(args, style, bundle, lambda jp: cli._new_approval(
                bundle.plan["hash"], [d["serial"] for d in bundle.ready()], bundle.acks(), jp, "typed APPROVE on the upgrade screen"))
        self.assertEqual(code, 0)
        [journal] = [n for n in os.listdir(self.runs) if n.endswith(".jsonl")]
        self.assertTrue(os.path.exists(os.path.join(self.runs, journal + ".approval.json")))

    def test_declining_in_the_approver_stops_before_anything_is_done(self):
        args = self.args()
        style = cli._style(args)
        with redirect_stdout(io.StringIO()):
            bundle = cli._upgrade_review(args, style)
            self.assertEqual(cli._upgrade_execute(args, style, bundle, lambda jp: None), 3)

    def test_an_earlier_unfinished_run_is_found_and_never_prompted_for(self):
        first = self.args("--sim-fail", "A1:install")                          # a run that fails part way leaves an unfinished journal
        style = cli._style(first)

        def approver(bundle):
            return lambda jp: cli._new_approval(bundle.plan["hash"], [d["serial"] for d in bundle.ready()], bundle.acks(), jp, "test")
        with redirect_stdout(io.StringIO()):
            bundle = cli._upgrade_review(first, style)
            self.assertEqual(cli._upgrade_execute(first, style, bundle, approver(bundle)), 1)
            again = cli._upgrade_review(self.args(), style)
        self.assertIsNotNone(again.previous)
        self.assertIn("failed", again.previous[1])
        with redirect_stdout(io.StringIO()), mock.patch("builtins.input", side_effect=AssertionError("the screen must never read the keyboard")):
            with self.assertRaisesRegex(ValueError, "--resume"):
                cli._upgrade_execute(self.args(), style, again, approver(again))
            fresh = self.args("--fresh")
            self.assertEqual(cli._upgrade_execute(fresh, style, cli._upgrade_review(fresh, style), approver(again)), 0)


class NothingStartsByItself(ShellCase):
    """Opening a screen is looking, not doing: every step that talks to Panorama waits for Start."""

    def test_opening_the_report_the_plan_prepare_and_upgrade_starts_nothing(self):
        ui = self.make()
        for key in ("alt-r", "alt-p", "alt-5", "alt-6"):
            ui.press(key)
            time.sleep(0.05)
            ui.tick()
            self.assertEqual(ui.running_jobs(), [], key)
        self.assertEqual((self.commands, self.reviews, self.up_reviews), ([], [], []))

    def test_each_one_says_so_and_offers_start_first(self):
        ui = self.make()
        ui.press("alt-r")
        self.assertIn("Press Start to read it", self.text(ui))
        self.assertEqual([label for label, _ in ui.screen.buttons()], ["Start", "Change sign-in", "Home"])
        ui.press("alt-p")
        self.assertEqual(ui.screen.buttons()[0][0], "Start")
        for key in ("alt-5", "alt-6"):
            ui.press(key)
            self.assertEqual(ui.screen.buttons()[0][0], "Start: read the plan")
            self.assertIn("[ Start: read the plan ]", self.text(ui, 120, 30))

    def test_the_home_screen_does_not_pretend_anything_was_read(self):
        ui = self.make()
        text = self.text(ui)
        self.assertIn("not read yet - open it, then press Start", text)
        self.assertIn("not started - open it, then press Start", text)

    def test_a_hotkey_then_enter_is_what_starts_it(self):
        ui = self.make()
        ui.press("alt-r"); ui.press("enter"); wait(ui)
        self.assertEqual(self.commands, [("report", None)])

    def test_the_local_release_table_needs_no_network_so_it_still_opens_ready(self):
        ui = self.make()
        ui.press("alt-8"); wait(ui)
        self.assertEqual(self.commands, [("releases", None)])

    def test_returning_from_fixing_the_sign_in_carries_on_with_the_read_the_person_started(self):
        calls = []

        def run(args):
            calls.append(1)
            if len(calls) == 1:
                args._sign_in_problem = "no usable account"
                return 2
            print("report after signing in")
            return 0
        ui = self.make(run=run)
        ui.press("alt-r"); ui.press("enter"); wait(ui)
        self.assertEqual(ui.screen.name, "auth")
        ui.screen.form.zone = "accounts"
        for key in ("r", "ctrl-d"):
            ui.press(key)
        wait(ui)
        self.assertEqual(len(calls), 2)
        ui.press("alt-h"); ui.press("alt-r")
        self.assertEqual(len(calls), 2)                        # the retry happens once, on arrival from the sign-in screen, not on every visit


class RefreshBecomesAbort(ShellCase):
    def reader(self):
        gate = threading.Event()

        def run(args):
            n = 0
            while not gate.is_set() and n < 300:
                progress.say(f"asking call {n}")
                n += 1
                time.sleep(0.01)
            print("the answer")
            return 0
        return gate, run

    def test_while_it_reads_the_first_button_is_abort_and_there_is_no_refresh(self):
        gate, run = self.reader()
        ui = self.make(run=run)
        ui.press("alt-r"); ui.press("enter")
        labels = [label for label, _ in ui.screen.buttons()]
        self.assertEqual(labels, ["Abort", "Change sign-in", "Home"])
        self.assertNotIn("Refresh", self.text(ui))
        ui.press("r")                                              # R does nothing while reading
        self.assertEqual(len(ui.jobs), 1)
        gate.set(); wait(ui)
        self.assertEqual(ui.screen.buttons()[0][0], "Refresh")

    def test_abort_stops_the_read_before_its_next_call_and_nothing_is_kept(self):
        gate, run = self.reader()
        ui = self.make(run=run)
        ui.press("alt-r"); ui.press("enter"); time.sleep(0.1)
        ui.press("enter")                                          # the first button is Abort now
        self.assertIn("Aborting", self.text(ui))
        wait(ui)
        text = self.text(ui)
        self.assertIn("Aborted. Nothing was read.", text)
        self.assertIsNone(ui.screens["report"].record())
        self.assertEqual(ui.screen.buttons()[0][0], "Start")
        self.assertTrue(ui.jobs[0].aborted)
        self.assertIn("!!", self.text(ui, 130, 30))                # the Jobs list shows how it ended

    def test_aborting_a_refresh_keeps_the_earlier_answer(self):
        gate, run = self.reader()
        gate.set()
        runs = []

        def both(args):
            runs.append(1)
            if len(runs) == 1:
                print("first answer")
                return 0
            return run(args)
        ui = self.make(run=both)
        ui.press("alt-r"); ui.press("enter"); wait(ui)
        gate.clear()
        ui.press("enter"); time.sleep(0.1)                         # Refresh
        ui.press("enter")                                          # Abort
        wait(ui)
        text = self.text(ui)
        self.assertIn("first answer", text)
        self.assertIn("The earlier result is still shown", text)

    def test_a_broad_except_exception_in_the_work_cannot_swallow_the_abort(self):
        def stubborn(args):
            for i in range(300):
                try:
                    progress.say(f"call {i}")
                except Exception:
                    pass
                time.sleep(0.01)
            return 0
        ui = self.make(run=stubborn)
        ui.press("alt-r"); ui.press("enter"); time.sleep(0.1)
        ui.press("enter")
        wait(ui)
        self.assertTrue(ui.jobs[0].aborted)

    def test_work_that_changes_firewalls_cannot_be_aborted_only_stopped(self):
        gate = threading.Event()
        job = shell.Job(lambda: gate.wait(3) and 0, "changing", changes=True)
        self.assertFalse(job.abort())
        self.assertFalse(job.abort_requested)
        gate.set()
        hold = threading.Event()
        quiet = shell.Job(lambda: hold.wait(3) and 0, "reading")
        self.assertTrue(quiet.abort())
        hold.set()


class TheChangeScreensToo(ShellCase):
    def test_reading_the_plan_can_be_aborted_and_nothing_is_left_half_done(self):
        gate = threading.Event()

        def review(args):
            for i in range(300):
                if gate.is_set():
                    break
                progress.say(f"asking call {i}")
                time.sleep(0.01)
            return FakeBundle()
        ui = self.make(review=review)
        ui.press("alt-5"); ui.press("enter"); time.sleep(0.1)
        self.assertEqual([label for label, _ in ui.screen.buttons()], ["Abort", "Home (it keeps reading)"])
        ui.press("enter")
        wait(ui)
        self.assertEqual(ui.screen.stage, "idle")
        self.assertIn("Aborted. Nothing was changed.", self.text(ui))
        self.assertEqual(self.executed, [])

    def test_a_real_run_offers_stop_and_a_rehearsal_offers_abort(self):
        gate = threading.Event()
        ui = self.make(execute=lambda a, b, c: gate.wait(3) and 0, run=lambda a: gate.wait(3) and 0)
        ui.press("alt-5"); ui.press("enter"); wait(ui)
        ui.press("enter")
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        self.assertEqual(ui.screen.buttons()[0][0], "Stop after the steps in progress")
        gate.set(); wait(ui)
        gate.clear()
        ui.press("enter")                                          # Read the plan again
        wait(ui)
        ui.press("right"); ui.press("enter")                       # Rehearse
        self.assertEqual(ui.screen.buttons()[0][0], "Abort")
        gate.set(); wait(ui)

    def test_stop_on_prepare_stops_it_starting_anything_new(self):
        started, release = threading.Event(), threading.Event()

        def execute(args, bundle, approve):
            started.set()
            bundle.stop.wait(3)
            return 0 if bundle.stop.is_set() else 1
        ui = self.make(execute=execute)
        ui.press("alt-5"); ui.press("enter"); wait(ui)
        ui.press("enter")
        for ch in "APPROVE":
            ui.press(ch)
        ui.press("enter")
        started.wait(3)
        ui.press("enter")                                          # Stop
        wait(ui)
        self.assertIn("finished at", self.text(ui))
        self.assertTrue(self.executed[0][0].stop.is_set())


class TheApprovalRecord(Home):
    def test_it_is_bound_to_the_plan_and_written_next_to_the_journal(self):
        journal = os.path.join(self.tmp.name, "prepare-1.jsonl")
        approval = cli._new_approval("ff" * 32, ["S1"], [], journal, "typed APPROVE on the prepare screen")
        self.assertEqual((approval.plan_hash, approval.serials), ("ff" * 32, ["S1"]))
        with open(journal + ".approval.json", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["plan_hash"], "ff" * 32)
        self.assertIn("typed APPROVE on the prepare screen", saved["approved_by"])


class Jobs(unittest.TestCase):
    def test_a_job_captures_what_is_printed_and_its_return_code(self):
        job = shell.Job(lambda: print("hello") or 3)
        end = time.monotonic() + 3
        while job.running and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual((job.text, job.code), ("hello\n", 3))

    def test_progress_words_are_per_thread_and_do_nothing_when_nobody_listens(self):
        progress.say("nobody hears this")                        # no listener on this thread: nothing happens
        job = shell.Job(lambda: progress.say("working on it") or time.sleep(0.2))
        time.sleep(0.1)
        self.assertEqual(job.latest(), "working on it")
        self.assertIsNone(getattr(progress._local, "callback", None))


if __name__ == "__main__":
    unittest.main()
