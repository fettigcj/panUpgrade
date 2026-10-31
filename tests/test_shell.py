# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The one-screen workflow: home, hotkeys between steps, background reads, and the way back from a sign-in problem."""

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from panupgrade import cli, credstore, profiles, progress, shell, site, trust, tui
from panupgrade.trust import CertificateProblem
from panupgrade.model import DeviceRef
from panupgrade.sources import SourceError
from tests.test_auth import Home


def ref(serial, name, version="10.2.1"):
    return DeviceRef(serial=serial, hostname=name, model="PA-VM", sw_version=version, connected=True, device_group="lab", ha_state="active")


REFS = [ref("S1", "fw-one"), ref("S2", "fw-two")]


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
        self.commands, self.reads = [], []

    def make(self, address="192.0.2.1", target="12.1.10", run=None, read=None):
        def run_command(args):
            self.commands.append((args.command, getattr(args, "target", None)))
            return run(args) if run else (print(f"output of {args.command}") or 0)

        def read_devices(args):
            self.reads.append(args.command)
            return read(args) if read else REFS
        session = shell.Session(cli.parse_args(["shell"]), address, target, self.lab)
        return shell.Shell(session, run_command=run_command, read_devices=read_devices)

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
        for _ in range(5):                                    # profile, address, target, then step 1 (sign in), 2 (firewalls) and 3: the fleet report
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
        ui.press("alt-r"); wait(ui)
        self.assertIn("read at", self.text(ui.goto("home") or ui))
        ui.press("right")
        self.assertIn("not read yet", self.text(ui))
        ui.press("alt-r"); wait(ui)
        self.assertEqual(len(self.commands), 2)                   # it read again, for the other Panorama

    def test_a_step_runs_in_the_profile_in_use(self):
        seen = []
        ui = self.make(run=lambda args: seen.append((args._profile.name, args.source)) or 0)
        ui.press("alt-r"); wait(ui)
        ui.press("alt-h"); ui.press("right")
        ui.press("alt-r"); wait(ui)
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
        ui.press("alt-r")                                        # leaving the screen keeps them
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
        ui.press("alt-r")
        self.assertEqual(ui.screen.name, "settings")
        self.assertIn("unsaved changes", ui.message)
        ui.press("alt-r")
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
        ui.press("alt-r")
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
        ui.press("alt-r"); wait(ui)
        ui.press("alt-h"); ui.press("alt-r")
        self.assertEqual(len(self.commands), 1)
        ui.press("r"); wait(ui)
        self.assertEqual(len(self.commands), 2)

    def test_a_different_address_reads_again(self):
        ui = self.make()
        ui.press("alt-r"); wait(ui)
        ui.press("alt-h")
        ui.session.address = "192.0.2.9"
        ui.press("alt-r"); wait(ui)
        self.assertEqual(len(self.commands), 2)

    def test_without_an_address_it_says_where_to_type_one_and_reads_nothing(self):
        ui = self.make(address="")
        ui.press("alt-r")
        self.assertIn("type its address, on the home screen", self.text(ui))
        self.assertEqual(self.commands, [])

    def test_the_action_bar_changes_sign_in_or_goes_home(self):
        ui = self.make()
        ui.press("alt-r"); wait(ui)
        ui.press("right"); ui.press("enter")
        self.assertEqual(ui.screen.name, "auth")
        self.assertEqual(ui.after_sign_in, "report")
        ui.press("alt-r"); wait(ui)
        ui.press("right"); ui.press("right"); ui.press("enter")
        self.assertEqual(ui.screen.name, "home")

    def test_long_output_scrolls(self):
        ui = self.make(run=lambda args: print("\n".join(f"line {i}" for i in range(100))) or 0)
        ui.press("alt-r"); wait(ui)
        self.assertNotIn("line 90", self.text(ui, 110, 20))
        ui.press("end")
        self.assertIn("line 99", self.text(ui, 110, 20))
        ui.press("home")
        self.assertIn("line 0", self.text(ui, 110, 20))
        self.assertIn("of 100", self.text(ui, 110, 20))

    def test_an_error_is_shown_not_hidden(self):
        ui = self.make(run=lambda args: print("error: Panorama said no", file=__import__("sys").stderr) or 2)
        ui.press("alt-r"); wait(ui)
        text = self.text(ui)
        self.assertIn("Panorama said no", text)
        self.assertIn("with an error", text)

    def test_a_bug_in_a_step_shows_up_on_the_screen_instead_of_ending_the_program(self):
        def boom(args):
            raise RuntimeError("kaboom")
        ui = self.make(run=boom)
        ui.press("alt-r"); wait(ui)
        self.assertIn("internal error: RuntimeError: kaboom", self.text(ui))

    def test_trouble_a_person_can_act_on_is_reported_plainly(self):
        def trouble(args):
            raise SourceError("cannot reach 192.0.2.1")
        ui = self.make(run=trouble)
        ui.press("alt-r"); wait(ui)
        self.assertIn("error: cannot reach 192.0.2.1", self.text(ui))
        self.assertNotIn("Traceback", self.text(ui))


class ThePlan(ShellCase):
    def test_it_needs_a_target_and_passes_it_on(self):
        ui = self.make(target="")
        ui.press("alt-p")
        self.assertIn("Type the target PAN-OS version", self.text(ui))
        self.assertEqual(self.commands, [])
        ui.press("alt-h"); ui.session.target = "10.2.11-h1"
        ui.press("alt-p"); wait(ui)
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
        ui.press("alt-r"); wait(ui)
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
        ui.press("alt-r"); wait(ui)
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
        ui.press("alt-r"); wait(ui)
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
        ui.press("alt-r"); wait(ui)
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
    def test_prepare_and_upgrade_say_how_to_run_them_for_now_with_the_address_and_target_filled_in(self):
        ui = self.make(address="pano.example", target="10.2.11-h1")
        ui.press("alt-5")
        text = self.text(ui)
        self.assertIn("panupgrade prepare --profile lab --target 10.2.11-h1", text)
        self.assertIn("not on the screen yet", text)
        ui.press("enter")
        ui.press("alt-6")
        self.assertIn("panupgrade upgrade --profile lab --target 10.2.11-h1", self.text(ui))

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
        ui.press("alt-r"); wait(ui)
        ui.press("alt-a"); ui.press("esc")                    # hop to the sign-in screen and home again
        ui.press("alt-f"); ui.press("alt-h")
        ui.press("alt-r")
        text = self.text(ui)
        self.assertIn("Needs attention (3 firewalls)", text)
        self.assertIn("result from", text)
        self.assertIn("just now", text)
        self.assertEqual(len(self.commands), 1)

    def test_leaving_while_it_is_still_reading_the_answer_is_waiting_when_you_come_back(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("Needs attention (2 firewalls)") or 0)
        ui.press("alt-r")
        ui.press("alt-h")                                     # off to the home screen while Panorama is still being asked
        gate.set()
        wait(ui)                                              # the loop's ticks run on whatever screen is in front
        self.assertIn("2 firewalls need attention", self.text(ui))
        ui.press("alt-r")
        self.assertIn("Needs attention (2 firewalls)", self.text(ui))
        self.assertEqual(len(self.commands), 1)

    def test_a_job_that_finishes_between_two_checks_still_gets_its_answer_without_a_key_press(self):
        ui = self.make()
        ui.press("alt-r")
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
        ui.press("alt-r"); wait(ui)
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
        ui.press("alt-r"); ui.press("r"); ui.press("r")
        gate.set(); wait(ui)
        self.assertEqual(len(self.commands), 1)

    def test_each_profile_keeps_its_own_answer_and_switching_back_shows_it_again(self):
        other = profiles.create("other", "192.0.2.77")
        credstore.write(other.creds_file, {"t": "pw"})
        ui = self.make(run=lambda args: print(f"report for {args.profile}") or 0)
        ui.press("alt-r"); wait(ui)
        ui.press("alt-h")
        ui.screen.at = 0
        ui.press("right")                                      # on to the next profile
        ui.press("alt-r"); wait(ui)
        self.assertIn("report for other", self.text(ui))
        ui.press("alt-h"); ui.screen.at = 0; ui.press("left")
        ui.press("alt-r")
        self.assertIn("report for lab", self.text(ui))
        self.assertEqual(len(self.commands), 2)

    def test_a_sign_in_problem_found_while_you_were_elsewhere_is_dealt_with_when_you_return(self):
        gate = threading.Event()

        def run(args):
            gate.wait(3)
            args._sign_in_problem = "no usable account"
            return 2
        ui = self.make(run=run)
        ui.press("alt-r"); ui.press("alt-h")
        gate.set(); wait(ui)
        self.assertEqual(ui.screen.name, "home")                # nobody is yanked away from what they were doing
        self.assertIn("sign in again", ui.render(110, 30)[-1][0][1])
        ui.press("alt-r")
        self.assertEqual(ui.screen.name, "auth")
        self.assertIn("no usable account", self.text(ui))


class TheJobsScreenAndPane(ShellCase):
    def test_every_read_is_listed_with_its_state_newest_first(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("done") or 0)
        ui.press("alt-r"); ui.press("alt-j")
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
        ui.press("alt-r"); wait(ui)
        ui.press("alt-j")
        text = self.text(ui, 130, 30)
        self.assertIn("!! Fleet report (lab)", text)
        self.assertIn("Panorama said no", text)
        self.assertIn("failed", text)

    def test_up_and_down_choose_a_job_and_the_output_follows(self):
        ui = self.make(run=lambda args: print(f"output for {args.command}") or 0)
        ui.press("alt-r"); wait(ui)
        ui.press("alt-p"); wait(ui)
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
        ui.press("alt-r"); wait(ui)
        ui.press("alt-h")
        wide = self.text(ui, 130, 30)
        self.assertIn("Background jobs", wide)
        self.assertIn("ok Fleet report (lab)", wide)
        self.assertIn("Panorama profile", wide)                           # the screen itself is still there
        self.assertNotIn("Background jobs", self.text(ui, 100, 30))
        for line in ui.render(130, 30):
            self.assertLessEqual(sum(len(t) for _s, t in line), 130)

    def test_the_forms_keep_their_room_and_only_share_a_very_wide_window(self):
        ui = self.make(run=lambda args: print("x") or 0)
        ui.press("alt-r"); wait(ui)
        ui.press("alt-a")
        self.assertNotIn("Background jobs", self.text(ui, 140, 36))
        self.assertIn("last sign-in test", self.text(ui, 140, 36))           # the whole table is there
        self.assertIn("Background jobs", self.text(ui, 175, 36))
        ui.press("alt-s")
        self.assertNotIn("Background jobs", self.text(ui, 140, 36))

    def test_a_running_job_is_marked_differently_from_the_separator(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("x") or 0)
        ui.press("alt-r"); ui.press("alt-h")
        self.assertIn(">> Fleet report (lab)", self.text(ui, 130, 30))
        gate.set(); wait(ui)

    def test_a_narrow_window_says_in_the_footer_that_jobs_are_running(self):
        gate = threading.Event()
        ui = self.make(run=lambda args: gate.wait(3) and print("x") or 0)
        ui.press("alt-r"); ui.press("alt-h")
        self.assertIn("1 job running", self.text(ui, 100, 30).splitlines()[-1])
        gate.set(); wait(ui)
        self.assertNotIn("job running", self.text(ui, 100, 30).splitlines()[-1])

    def test_the_home_screen_summarises_the_jobs(self):
        ui = self.make()
        self.assertIn("nothing has run yet", self.text(ui))
        ui.press("alt-r"); wait(ui); ui.press("alt-h")
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
        ui.press("alt-r")
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
        ui.press("alt-r"); ui.press("alt-p")
        time.sleep(0.1)
        gates["plan"].set(); gates["report"].set()
        wait(ui)
        by_title = {job.title: job.text for job in ui.jobs}
        self.assertEqual(by_title["Fleet report (lab)"], "start report\nend report\n")
        self.assertEqual(by_title["Plan the upgrade (lab)"], "start plan\nend plan\n")

    def test_the_real_streams_are_put_back_once_the_work_is_done(self):
        before = (__import__("sys").stdout, __import__("sys").stderr)
        ui = self.make()
        ui.press("alt-r"); wait(ui)
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
