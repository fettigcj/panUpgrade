# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The settings form: every setting on one screen, edited with the keyboard, saved as only what differs from the defaults."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, fields
from pathlib import Path
from unittest import mock

from panupgrade import cli, settings, settingsform, tui
from panupgrade.settings import Settings


def keys(form, *presses):
    for p in presses:
        form.press(p)


def typing(text):
    return list(text)


def goto(form, name):
    form.zone, form.focus = "rows", form.names.index(name)


def screen(form, width=120, height=40):
    return tui.plain(form.render(width, height, "title here"))


def fresh(**over):
    return settingsform.Form(settings.load(None, [f"{k}={v}" for k, v in over.items()]), "settings.toml", saver=lambda f, c: "saved")


class Coverage(unittest.TestCase):
    def test_every_setting_is_on_the_form(self):
        names = {f.name for f in fields(Settings)}
        self.assertEqual(set(settingsform.shown_names()), names)
        self.assertEqual(len(settingsform.shown_names()), len(set(settingsform.shown_names())))

    def test_every_shown_setting_is_explained_and_every_choice_has_a_meaning(self):
        for name in settingsform.shown_names():
            self.assertIn(name, settingsform.INFO, name)
        for name, choices in settings._CHOICES.items():
            self.assertEqual(set(settingsform.INFO[name][1]), set(choices), name)


class Editing(unittest.TestCase):
    def test_it_opens_on_the_first_setting_with_nothing_changed(self):
        form = fresh()
        self.assertEqual((form.zone, form.focus), ("rows", 0))
        self.assertEqual(form.changed_count(), 0)
        self.assertFalse(form.dirty())

    def test_up_down_and_tab_walk_the_settings_then_the_buttons_and_wrap(self):
        form = fresh()
        for _ in form.names:
            form.press("tab")
        self.assertEqual(form.zone, "buttons")
        form.press("tab")
        self.assertEqual((form.zone, form.focus), ("rows", 0))
        form.press("shift-tab")
        self.assertEqual(form.zone, "buttons")
        form.press("up")
        self.assertEqual((form.zone, form.focus), ("rows", len(form.names) - 1))

    def test_left_and_right_step_through_a_choice_and_wrap(self):
        form = fresh()
        goto(form, "sync_to_peer")
        self.assertEqual(form.text["sync_to_peer"], "Both")
        keys(form, "right")
        self.assertEqual(form.text["sync_to_peer"], "UpgradeOnly")
        keys(form, "right")
        self.assertEqual(form.text["sync_to_peer"], "Disable")
        keys(form, "left")
        self.assertEqual(form.text["sync_to_peer"], "UpgradeOnly")

    def test_a_yes_no_setting_toggles(self):
        form = fresh()
        goto(form, "allow_downgrade")
        keys(form, "right")
        self.assertEqual(form.text["allow_downgrade"], "true")
        keys(form, " ")
        self.assertEqual(form.text["allow_downgrade"], "false")

    def test_numbers_are_typed_and_the_form_knows_what_changed(self):
        form = fresh()
        goto(form, "max_parallel")
        keys(form, "backspace", *typing("3"))
        self.assertEqual(form.settings().max_parallel, 3)
        self.assertTrue(form.changed("max_parallel"))
        self.assertEqual(form.changed_count(), 1)
        self.assertTrue(form.dirty())
        self.assertIn("* max_parallel", screen(form))

    def test_text_that_is_not_a_number_is_flagged_and_stops_a_save(self):
        form = fresh()
        goto(form, "max_failures")
        keys(form, "backspace", *typing("x"))
        self.assertIsNone(form.settings())
        self.assertIn("max_failures: must be a whole number", screen(form))
        form.zone = "buttons"
        form.button = settingsform.SAVE_EXIT
        self.assertIsNone(form.press("enter"))
        self.assertEqual((form.zone, form.focus), ("rows", form.names.index("max_failures")))     # takes you to the problem

    def test_a_rule_between_two_settings_is_blamed_on_the_one_you_can_fix(self):
        form = fresh()
        goto(form, "allow_cross_feature_downgrade")
        keys(form, "right")
        self.assertIsNone(form.settings())
        self.assertIn("allow_cross_feature_downgrade", form.check()[1])
        keys(form, "tab", "up")                                  # (anything) then back; the error stays until the pair is consistent
        goto(form, "allow_downgrade")
        keys(form, "right")
        self.assertIsNotNone(form.settings())

    def test_out_of_range_values_are_caught_by_the_same_rules_the_command_line_uses(self):
        form = fresh()
        goto(form, "failover_band")
        keys(form, "ctrl-u", *typing("1.5"))
        self.assertIsNone(form.settings())
        self.assertIn("failover_band", form.check()[1])

    def test_ctrl_r_and_delete_put_a_setting_back_to_its_default(self):
        form = fresh(max_parallel=4)
        goto(form, "max_parallel")
        self.assertTrue(form.changed("max_parallel"))
        keys(form, "ctrl-r")
        self.assertEqual(form.text["max_parallel"], "1")
        self.assertFalse(form.changed("max_parallel"))

    def test_typing_in_a_choice_does_nothing_rather_than_corrupting_it(self):
        form = fresh()
        goto(form, "restore_roles")
        keys(form, *typing("zzz"), "backspace")
        self.assertEqual(form.text["restore_roles"], "leave")

    def test_the_detail_explains_the_setting_and_the_value_it_has(self):
        form = fresh()
        goto(form, "sync_to_peer")
        text = screen(form)
        self.assertIn("one of: Disable, Both, UpgradeOnly", text)
        flat = " ".join(text.split())
        self.assertIn("Both: the default. Force the sync when preparing for the upgrade cycle (`prepare`) AND again during the upgrade "
                      "itself", flat)
        keys(form, "right")
        self.assertIn("UpgradeOnly: force the sync only during the upgrade itself", " ".join(screen(form).split()))

    def test_nothing_is_cut_off_with_dots_every_word_of_the_explanation_is_on_screen(self):
        import re
        for width in (70, 100, 160):
            for name in settingsform.shown_names():
                form = fresh(pending_changes="ignore", allow_downgrade="true", max_parallel=3)
                goto(form, name)
                text = " ".join(screen(form, width, 60).split())
                description, meanings = settingsform.INFO[name]
                for passage in [description] + ([meanings[form.text[name]]] if form.text[name] in meanings else []):
                    self.assertIn(" ".join(passage.split()), text, f"{name} at width {width}")
                self.assertNotIn("...", re.sub(r"\.\.\. and \d+ more", "", text.replace("(...)", "")), f"{name} at width {width}")

    def test_a_long_value_is_shown_in_full_below_the_list(self):
        form = fresh(backup_dir="D:/a/very/long/folder/name/for/backups")
        goto(form, "backup_dir")
        self.assertIn("Value: D:/a/very/long/folder/name/for/backups", " ".join(screen(form).split()))

    def test_risky_settings_show_a_caution_wherever_the_cursor_is(self):
        form = fresh(pending_changes="ignore")
        goto(form, "spread_limit")
        self.assertIn("Caution: pending_changes=ignore", screen(form))

    def test_a_short_window_scrolls_to_keep_the_current_setting_in_view(self):
        form = fresh()
        goto(form, "approval_max_age_hours")
        self.assertRegex(screen(form, 100, 22), r"approval_max_age_hours\s+\[168")
        self.assertLessEqual(len(form.render(100, 22)), 22)

    def test_no_line_is_wider_than_the_window(self):
        for width in (60, 90, 140):
            for line in fresh(allow_downgrade="true").render(width, 40):
                self.assertLess(len("".join(t for _s, t in line)), width)


class Buttons(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.form = settingsform.Form(Settings(), "s.toml", saver=lambda f, c: self.saved.append(asdict(c)) or "Saved.")
        goto(self.form, "max_parallel")
        keys(self.form, "backspace", *typing("2"))

    def press_button(self, n):
        self.form.zone, self.form.button = "buttons", n
        return self.form.press("enter")

    def test_save_writes_now_stays_open_and_clears_the_marker(self):
        self.assertIn("unsaved changes", screen(self.form))
        self.assertIsNone(self.press_button(settingsform.SAVE_ONLY))
        self.assertEqual(self.saved[0]["max_parallel"], 2)
        self.assertFalse(self.form.dirty())
        self.assertEqual(self.form.message, "Saved.")
        self.assertNotIn("warn", [s for s, _t in self.form.render(120, 40)[-1]])

    def test_save_and_exit_saves_then_finishes(self):
        self.assertEqual(self.press_button(settingsform.SAVE_EXIT), "accept")
        self.assertEqual(len(self.saved), 1)

    def test_quit_leaves_at_once_and_saves_nothing(self):
        self.assertEqual(self.press_button(settingsform.QUIT), "cancel")
        self.assertEqual(self.saved, [])

    def test_ctrl_d_is_save_and_exit_and_esc_asks_twice_while_there_are_unsaved_changes(self):
        self.assertIsNone(self.form.press("esc"))
        self.assertIn("unsaved changes", self.form.message)
        self.assertEqual(self.form.press("esc"), "cancel")
        self.assertEqual(self.form.press("ctrl-d"), "accept")
        self.assertEqual(settingsform.Form(Settings(), "s.toml").press("esc"), "cancel")

    def test_a_failed_write_says_why_and_stays_open(self):
        def broken(form, chosen):
            raise PermissionError(13, "Permission denied")
        form = settingsform.Form(Settings(), "s.toml", saver=broken)
        goto(form, "max_parallel")
        keys(form, "backspace", *typing("2"))
        form.zone, form.button = "buttons", settingsform.SAVE_ONLY
        self.assertIsNone(form.press("enter"))
        self.assertIn("Could not save: Permission denied", form.message)
        self.assertTrue(form.dirty())

    def test_edits_made_on_the_command_line_count_as_unsaved(self):
        form = settingsform.Form(settings.load(None, ["max_parallel=2"]), "s.toml", baseline=Settings())
        self.assertTrue(form.dirty())


class WritingTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "settings.toml")

    def test_only_what_differs_from_the_defaults_is_written_and_it_loads_back_the_same(self):
        chosen = settings.load(None, ["max_parallel=3", "pending_changes=warn", "allow_downgrade=true", "failover_band=0.4", "backup_dir=D:/b"])
        settingsform.write_file(self.path, chosen)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("max_parallel = 3", text)
        self.assertIn('pending_changes = "warn"', text)
        self.assertIn("allow_downgrade = true", text)
        self.assertNotIn("spread_limit", text)
        self.assertEqual(asdict(settings.load(self.path)), asdict(chosen))

    def test_all_defaults_gives_a_valid_empty_file(self):
        settingsform.write_file(self.path, Settings())
        self.assertEqual(asdict(settings.load(self.path)), asdict(Settings()))

    def test_a_windows_path_survives_the_round_trip(self):
        chosen = settings.from_dict({"backup_dir": "C:\\Users\\someone\\backups"})
        settingsform.write_file(self.path, chosen)
        self.assertEqual(settings.load(self.path).backup_dir, "C:\\Users\\someone\\backups")


class SettingsVerb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.here)
        cli._SAID.clear()

    def args(self, **over):
        base = dict(interactive=None, set=None, settings=None, color="never", ascii=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_the_form_opens_by_itself_at_a_terminal_and_never_for_a_script(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(cli._wants_settings_form(self.args()))
            self.assertFalse(cli._wants_settings_form(self.args(set=["max_parallel=2"])))
            self.assertFalse(cli._wants_settings_form(self.args(interactive=False)))
            self.assertTrue(cli._wants_settings_form(self.args(interactive=True, set=["max_parallel=2"])))
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(cli._wants_settings_form(self.args()))
            with self.assertRaisesRegex(cli.SourceError, "needs a terminal"):
                cli._wants_settings_form(self.args(interactive=True))

    def test_without_a_terminal_it_prints_the_values_as_before(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["settings"]), 0)
        self.assertIn('"max_parallel": 1', out.getvalue())

    def run_form(self, fill, *extra):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=fill):
            code = cli.main(["settings", "--color", "never", "--ascii", *extra])
        return code, out.getvalue()

    def test_saving_from_the_form_writes_settings_toml_here_and_every_command_then_uses_it(self):
        def fill(form, title=""):
            goto(form, "max_parallel")
            keys(form, "backspace", *typing("3"))
            return form.press("ctrl-d") == "accept"
        code, text = self.run_form(fill)
        self.assertEqual(code, 0)
        self.assertIn("Settings saved to", text)
        self.assertIn("max_parallel = 3", Path(cli.DEFAULT_SETTINGS).read_text(encoding="utf-8"))
        self.assertIn("max_parallel=3: several HA pairs", text)
        a = argparse.Namespace(settings=None, set=None, color="never", ascii=True)
        said = io.StringIO()
        with redirect_stdout(said):
            self.assertEqual(cli._load_settings(a).max_parallel, 3)
        self.assertIn("found in this folder", said.getvalue())
        self.assertEqual(Path(a.settings).name, "settings.toml")                  # the kit script pins this exact file

    def test_without_a_settings_file_commands_use_the_built_in_defaults_and_say_so(self):
        a = argparse.Namespace(settings=None, set=None, color="never", ascii=True)
        said = io.StringIO()
        with redirect_stdout(said):
            self.assertEqual(cli._load_settings(a).max_parallel, 1)
        self.assertIn("built-in default settings", said.getvalue())

    def test_the_form_edits_an_existing_file_named_on_the_command_line(self):
        Path("mine.toml").write_text("max_parallel = 2\n", encoding="utf-8")
        seen = {}

        def fill(form, title=""):
            seen["parallel"], seen["note"], seen["title"] = form.text["max_parallel"], form.message, title
            return False
        code, text = self.run_form(fill, "--settings", "mine.toml")
        self.assertEqual(code, 1)
        self.assertEqual(seen["parallel"], "2")
        self.assertIn("Saving rewrites it", seen["note"])
        self.assertIn("mine.toml", seen["title"])
        self.assertFalse(os.path.exists(cli.DEFAULT_SETTINGS))

    def test_quitting_saves_nothing(self):
        code, text = self.run_form(lambda form, title="": False)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(cli.DEFAULT_SETTINGS))


if __name__ == "__main__":
    unittest.main()
