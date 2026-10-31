# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The one-screen workflow: home, hotkeys between steps, background reads, and the way back from a sign-in problem."""

import io
import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from panupgrade import cli, credstore, shell, site, trust, tui
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
        job = getattr(ui.screen, "job", None)
        if job is None or not job.running:
            ui.tick()
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
        credstore.write(self.home / "credentials.txt", {"tool": "pw"})
        self.commands, self.reads = [], []

    def make(self, address="192.0.2.1", target="12.1.10", run=None, read=None):
        def run_command(args):
            self.commands.append((args.command, getattr(args, "target", None)))
            return run(args) if run else (print(f"output of {args.command}") or 0)

        def read_devices(args):
            self.reads.append(args.command)
            return read(args) if read else REFS
        session = shell.Session(cli.parse_args(["shell"]), address, target)
        return shell.Shell(session, run_command=run_command, read_devices=read_devices)

    def text(self, ui, width=110, height=30):
        return tui.plain(ui.render(width, height))


class Home_(ShellCase):
    def test_it_opens_on_the_home_screen_with_the_steps_in_order_and_where_each_stands(self):
        ui = self.make()
        text = self.text(ui)
        for needle in ("Panorama address", "[192.0.2.1_]", "Target PAN-OS", "1  Sign in", "2  Fleet report", "3  Choose firewalls", "4  Plan the upgrade",
                       "5  Prepare", "6  Upgrade", "7  Settings", "8  PAN-OS releases", "Q  Quit", "Alt-R report"):
            self.assertIn(needle, text)
        self.assertIn("tool (password) reads", text)
        self.assertIn("built-in defaults", text)

    def test_the_address_and_target_are_typed_on_the_home_screen(self):
        ui = self.make(address="", target="")
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
        ui.press("down"); ui.press("down")                    # off the fields, onto step 1
        ui.press("down")                                      # step 2: the fleet report
        ui.press("enter")
        self.assertEqual(ui.screen.name, "report")
        wait(ui)
        ui.press("esc")
        self.assertEqual(ui.screen.name, "home")
        self.assertEqual(ui.press("esc"), "accept")           # esc on home quits
        ui2 = self.make()
        ui2.at = 0
        ui2.screen.at = len(ui2.screen.FIELDS) + len(shell.STEP_ORDER)
        self.assertEqual(ui2.press("enter"), "accept")

    def test_q_quits_from_the_step_list_but_is_just_a_letter_in_a_text_field(self):
        ui = self.make()
        ui.press("q")
        self.assertEqual(ui.session.address, "192.0.2.1q")
        ui.screen.at = 3
        self.assertEqual(ui.press("q"), "accept")


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
        ui.press("alt-9")
        self.assertEqual(ui.screen.name, shell.STEP_ORDER[-1])        # no ninth step: nothing happens
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
        self.assertIn("Type the Panorama address on the home screen", self.text(ui))
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
        self.assertEqual(cli._read_list("upgradeList.txt"), ["S1", "S2"])
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
        self.assertIn("Type the Panorama address", self.text(ui))
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
        self.assertIn("panupgrade prepare --panorama pano.example --target 10.2.11-h1", text)
        self.assertIn("not on the screen yet", text)
        ui.press("enter")
        ui.press("alt-6")
        self.assertIn("panupgrade upgrade --panorama pano.example --target 10.2.11-h1", self.text(ui))

    def test_settings_open_in_the_shell_and_save_to_settings_toml(self):
        ui = self.make()
        ui.press("alt-s")
        form = ui.screen.form
        form.zone, form.focus = "rows", form.names.index("max_parallel")
        form.press("backspace"); form.press("2")
        ui.press("ctrl-d")
        self.assertEqual(ui.screen.name, "home")
        self.assertIn("max_parallel = 2", open("settings.toml", encoding="utf-8").read())

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
        with mock.patch.dict(os.environ, {"PANUPGRADE_HOME": "."}):
            session = shell.Session(cli.parse_args(["shell"]), "192.0.2.1", "10.2.11-h1")
            ui = shell.Shell(session, run_command=lambda a: 0, read_devices=lambda a: REFS)
            known = set(tui.CODES)
            for name in shell.STEP_ORDER + ["home"]:
                ui.goto(name)
                for line in ui.render(110, 30):
                    for style, _text in line:
                        self.assertIn(style, known, f"{name} uses the style {style!r}")


class Jobs(unittest.TestCase):
    def test_a_job_captures_what_is_printed_and_its_return_code(self):
        job = shell.Job(lambda: print("hello") or 3)
        end = time.monotonic() + 3
        while job.running and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual((job.text, job.code), ("hello\n", 3))


if __name__ == "__main__":
    unittest.main()
