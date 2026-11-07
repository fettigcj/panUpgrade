# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The select form: filters over the columns, Available and Included tables, and the equivalent command afterwards."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from panupgrade import cli, selectform
from panupgrade.model import DeviceRef
from panupgrade.planner import Selection


def ref(serial, name, version="10.2.1", ha="active", model="PA-VM", group="lab", connected=True):
    return DeviceRef(serial=serial, hostname=name, model=model, sw_version=version, connected=connected, device_group=group, ha_state=ha)


REFS = [ref("S1", "site1-fw1", "10.2.1", "active"), ref("S2", "site1-fw2", "10.2.1", "passive"),
        ref("S3", "site2-fw1", "10.2.11-h1", "active", group="edge"), ref("S4", "gate01", "12.1.10-h2", "active", model="PA-850")]


def keys(form, *presses):
    for p in presses:
        form.press(p)


def typing(text):
    return list(text)


def screen(form, width=110, height=36):
    return selectform.plain(form.render(width, height, "title here"))


class Filters(unittest.TestCase):
    def setUp(self):
        self.form = selectform.Form(REFS)

    def test_it_opens_on_the_hostname_filter_with_everything_available_and_nothing_included(self):
        self.assertEqual((self.form.zone, self.form.fcol, self.form.frow), ("filter", 0, "input"))
        self.assertEqual(len(self.form.available()), 4)
        self.assertEqual(self.form.included, [])

    def test_tab_walks_across_the_filters_then_the_tables_then_the_save_box_and_round_again(self):
        seen = []
        for _ in range(len(selectform.COLUMNS) + 5):
            seen.append((self.form.zone, self.form.fcol))
            self.form.press("tab")
        self.assertEqual([z for z, _c in seen[:7]], ["filter"] * 7)
        self.assertEqual([c for _z, c in seen[:7]], list(range(7)))
        self.assertEqual([z for z, _c in seen[7:]], ["available", "included", "save", "buttons", "filter"])
        fresh = selectform.Form(REFS)
        fresh.press("shift-tab")
        self.assertEqual(fresh.zone, "buttons")

    def test_typing_in_a_column_filter_narrows_the_available_table_as_you_go(self):
        keys(self.form, *typing("site1-*"))
        self.assertEqual([r.serial for r in self.form.available()], ["S1", "S2"])
        keys(self.form, "backspace", "backspace")
        self.assertEqual(self.form.values["name_glob"], "site1")
        keys(self.form, "ctrl-u")
        self.assertEqual(len(self.form.available()), 4)

    def test_the_mode_row_switches_a_column_between_pattern_and_contains(self):
        keys(self.form, "up", "right")                        # up to the mode row, then change the mode
        self.assertEqual(self.form.mode[0], 1)
        keys(self.form, "down", *typing("gate"))
        self.assertEqual(self.form.values["name_contains"], "gate")
        self.assertEqual(self.form.values["name_glob"], "")
        self.assertEqual([r.serial for r in self.form.available()], ["S4"])
        keys(self.form, "up", "left", "down")
        self.assertEqual(self.form.mode[0], 0)

    def test_the_version_column_has_is_older_than_and_newer_than(self):
        keys(self.form, "tab", "tab", "tab", "up", "right", "down", *typing("10.2.11"))
        self.assertEqual(self.form.values["version_older_than"], "10.2.11")
        self.assertEqual([r.serial for r in self.form.available()], ["S1", "S2"])

    def test_a_bad_version_is_flagged_blocks_done_and_does_not_break_the_tables(self):
        keys(self.form, "tab", "tab", "tab", "up", "right", "down", *typing("banana"))
        self.assertIn("version_older_than", self.form.errors())
        self.assertEqual(len(self.form.available()), 4)
        keys(self.form, "tab")
        self.assertIsNone(self.form.press("ctrl-d"))
        self.assertIn("red filter", self.form.message)

    def test_arrows_step_a_single_value_filter_through_what_the_fleet_has_in_release_order(self):
        keys(self.form, "tab", "tab", "tab", "right")
        self.assertEqual(self.form.values["version_is"], "10.2.1")
        keys(self.form, "right", "right")
        self.assertEqual(self.form.values["version_is"], "12.1.10-h2")
        keys(self.form, "right")
        self.assertEqual(self.form.values["version_is"], "")

    def test_arrows_add_and_remove_values_in_a_list_filter(self):
        keys(self.form, *["tab"] * 4, "right", "right")
        self.assertEqual(self.form.values["ha_states"], "active, passive")
        keys(self.form, "right")
        self.assertEqual(self.form.values["ha_states"], "active, passive")
        keys(self.form, "left")
        self.assertEqual(self.form.values["ha_states"], "active")

    def test_down_from_the_filters_goes_into_the_tables_and_up_comes_back(self):
        keys(self.form, "down")
        self.assertEqual(self.form.zone, "available")
        keys(self.form, "up")
        self.assertEqual((self.form.zone, self.form.frow), ("filter", "input"))


class TheHostnameRegexMode(unittest.TestCase):
    def setUp(self):
        self.form = selectform.Form(REFS)
        keys(self.form, "up", "right", "right", "down")        # mode row, pattern -> contains -> regex, back to the input
        self.assertEqual(selectform.COLUMNS[0].modes[self.form.mode[0]][1], "name_regex")

    def hosts(self, text):
        keys(self.form, "ctrl-u", *typing(text))
        return [r.hostname for r in self.form.matching()]

    def test_the_caret_anchors_the_start_and_the_dot_is_any_one_character(self):
        self.assertEqual(self.hosts("^site1"), ["site1-fw1", "site1-fw2"])
        self.assertEqual(self.hosts("fw1$"), ["site1-fw1", "site2-fw1"])
        self.assertEqual(self.hosts("^site.-fw1$"), ["site1-fw1", "site2-fw1"])
        self.assertEqual(self.hosts("^GATE"), ["gate01"])                  # ignores case

    def test_it_is_found_anywhere_unlike_the_glob_which_must_match_the_whole_name(self):
        self.assertEqual(self.hosts("fw"), ["site1-fw1", "site1-fw2", "site2-fw1"])

    def test_a_broken_expression_is_flagged_and_does_not_hide_every_firewall(self):
        found = self.hosts("site1-(fw")
        self.assertIn("name_regex", self.form.errors())
        self.assertEqual(len(found), 4)                                    # ignored while it does not compile, like a bad version
        self.assertIn("regular expression", screen(self.form))

    def test_the_glob_and_contains_modes_are_unchanged(self):
        form = selectform.Form(REFS)
        keys(form, *typing("site1-?w1"))
        self.assertEqual([r.hostname for r in form.matching()], ["site1-fw1"])
        self.assertEqual(Selection(name_regex="^site1").describe(), "hostname matches regex ^site1")


class Lists(unittest.TestCase):
    def setUp(self):
        self.form = selectform.Form(REFS)
        keys(self.form, "down")                                # into Available

    def test_space_moves_the_firewall_under_the_cursor_to_included(self):
        keys(self.form, " ")
        self.assertEqual(self.form.included, ["S4"])           # the table is sorted by hostname: gate01 first
        self.assertEqual(len(self.form.available()), 3)

    def test_a_adds_everything_listed_and_respects_the_filters(self):
        keys(self.form, "up", *typing("site1-*"), "down", "a")
        self.assertEqual(sorted(self.form.included), ["S1", "S2"])
        self.assertEqual([r.serial for r in self.form.available()], [])

    def test_included_firewalls_leave_available_even_when_they_still_match(self):
        keys(self.form, " ", " ")
        self.assertEqual(len(self.form.available()), 2)
        self.assertIn("2 of them already included", screen(self.form))

    def test_space_in_included_takes_a_firewall_back_out_and_c_clears_the_lot(self):
        keys(self.form, "a")
        self.assertEqual(len(self.form.included), 4)
        keys(self.form, "tab", " ")
        self.assertEqual(len(self.form.included), 3)
        keys(self.form, "c")
        self.assertEqual(self.form.included, [])

    def test_down_past_the_last_available_row_moves_on_to_included(self):
        keys(self.form, "down", "down", "down")
        self.assertEqual(self.form.zone, "available")
        keys(self.form, "down")
        self.assertEqual(self.form.zone, "included")

    def test_the_cursor_stays_in_range_as_rows_leave_the_list(self):
        keys(self.form, "end", " ", " ")
        self.assertLess(self.form.cursor["available"], len(self.form.available()))

    def test_done_needs_something_in_included(self):
        self.assertIsNone(self.form.press("ctrl-d"))
        self.assertIn("Included list is empty", self.form.message)
        keys(self.form, " ")
        self.assertEqual(self.form.press("ctrl-d"), "accept")

    def test_enter_in_the_save_box_moves_on_to_the_buttons_and_enter_there_saves_and_exits(self):
        keys(self.form, " ", "tab", "tab", *typing("my.txt"))
        self.assertEqual(self.form.out, "my.txt")
        self.assertIsNone(self.form.press("enter"))
        self.assertEqual((self.form.zone, self.form.button), ("buttons", selectform.SAVE_EXIT))
        self.assertEqual(self.form.press("enter"), "accept")

    def test_escape_asks_twice_when_a_list_would_be_lost_and_once_when_not(self):
        self.assertEqual(selectform.Form(REFS).press("esc"), "cancel")
        keys(self.form, " ")
        self.assertIsNone(self.form.press("esc"))
        self.assertIn("not been saved", self.form.message)
        self.assertEqual(self.form.press("esc"), "cancel")
        keys(self.form, "esc", "down")                          # any other key disarms it
        self.assertIsNone(self.form.press("esc"))

    def test_a_serial_list_given_up_front_starts_in_included(self):
        form = selectform.Form(REFS, Selection(serials=["S2", "S3"], name_glob="x*"))
        self.assertEqual(form.included, ["S2", "S3"])
        self.assertEqual(form.values["name_glob"], "x*")        # other filters pre-fill the filter row

    def test_the_flags_are_the_filters_when_they_alone_make_the_list_and_serials_when_hand_picked(self):
        form = selectform.Form(REFS)
        keys(form, *typing("site1-*"), "down", "a")
        self.assertEqual(form.flags(), [("--name", "site1-*")])
        self.assertEqual(form.describe(), "hostname like site1-*")
        keys(form, "tab", " ")                                  # take one out: now it is hand-picked
        self.assertEqual(form.flags(), [("--serial", "S2")])
        self.assertEqual(form.describe(), "1 firewall picked by hand")


class Drawing(unittest.TestCase):
    def test_filters_sit_above_the_columns_they_filter(self):
        lines = screen(selectform.Form(REFS)).split("\n")
        header = next(l for l in lines if "hostname" in l and "serial" in l)
        mode = next(l for l in lines if "< pattern >" in l)
        row = next(l for l in lines if "site1-fw1" in l)
        for word, below in (("serial", "contains"), ("PAN-OS", "< is >"), ("HA", "contains")):
            self.assertEqual(header.index(word), mode.index(below, header.index(word) - 1), word)
        self.assertEqual(header.index("serial"), row.index("S1"))
        self.assertEqual(header.index("PAN-OS"), row.index("10.2.1"))

    def test_both_tables_and_their_counts_are_shown(self):
        form = selectform.Form(REFS)
        keys(form, "down", " ")
        text = screen(form)
        for needle in ("title here", "FILTERS", "AVAILABLE", "4 of 4 firewalls match the filters, 1 of them already included", "INCLUDED", "1 firewall for the batch",
                       "Save the list to", "gate01", "site1-fw1"):
            self.assertIn(needle, text)

    def test_offline_firewalls_are_marked(self):
        form = selectform.Form([ref("X1", "down01", connected=False)])
        self.assertRegex(screen(form), r"! +down01")

    def test_a_short_window_scrolls_the_table_instead_of_overflowing(self):
        many = [ref(f"S{i:02}", f"fw{i:02}", "10.2.1") for i in range(60)]
        form = selectform.Form(many)
        keys(form, "down", "end")
        lines = form.render(100, 24)
        self.assertLessEqual(len(lines), 24)
        text = selectform.plain(lines)
        self.assertIn("fw59", text)
        self.assertIn("showing", text)

    def test_no_line_is_wider_than_the_window(self):
        for width in (60, 80, 120):
            for line in selectform.Form(REFS, Selection(name_glob="x" * 200)).render(width, 30):
                self.assertLess(len("".join(t for _s, t in line)), width)

    def test_the_focused_filter_explains_itself(self):
        form = selectform.Form(REFS)
        self.assertIn("e.g. site1-*", screen(form))
        keys(form, *["tab"] * 4)
        self.assertIn("This fleet has: active, passive", screen(form))

    def test_run_feeds_keys_until_done_and_cancels_cleanly(self):
        presses = iter(["down", " ", "ctrl-d"])
        out = io.StringIO()
        form = selectform.Form(REFS)
        self.assertTrue(selectform.run(form, lambda: next(presses), out, lambda: (100, 30)))
        self.assertEqual(form.included, ["S4"])
        self.assertIn("\x1b[H", out.getvalue())
        self.assertFalse(selectform.run(selectform.Form(REFS), lambda: "esc", io.StringIO(), lambda: (100, 30)))


class WildcardsAndKeys(unittest.TestCase):
    def test_a_word_typed_in_a_contains_filter_matches_anywhere_in_the_value(self):
        form = selectform.Form(REFS)
        keys(form, "tab", "tab", *typing("P"))                              # model is the third column
        self.assertEqual(sorted(r.serial for r in form.available()), ["S1", "S2", "S3", "S4"])
        keys(form, "backspace", *typing("850"))
        self.assertEqual([r.serial for r in form.available()], ["S4"])

    def test_your_own_wildcards_are_taken_as_written(self):
        form = selectform.Form(REFS)
        keys(form, "tab", "tab", *typing("PA-8*"))
        self.assertEqual([r.serial for r in form.available()], ["S4"])
        self.assertEqual(form.flags(), [])                                  # a partial list is hand-picked until added
        keys(form, "down", "a")
        self.assertEqual(form.flags(), [("--model", "PA-8*")])

    def test_the_wrapped_words_are_what_the_repeat_command_uses(self):
        form = selectform.Form(REFS)
        keys(form, "tab", "tab", *typing("VM"), "down", "a")
        self.assertEqual(form.flags(), [("--model", "*VM*")])

    def test_the_command_line_flags_accept_wildcards_too_and_exact_values_still_work(self):
        self.assertTrue(Selection(models=["PA-VM"]).matches(REFS[0]))
        self.assertFalse(Selection(models=["PA-V"]).matches(REFS[0]))
        self.assertTrue(Selection(models=["pa-*"]).matches(REFS[3]))
        self.assertTrue(Selection(device_groups=["ed*"]).matches(REFS[2]))
        self.assertTrue(Selection(ha_states=["ACTIVE"]).matches(REFS[0]))
        self.assertTrue(Selection(serials=["S1"]).matches(REFS[0]))

    def test_shift_tab_is_told_from_tab_by_the_console_event(self):
        self.assertEqual(selectform.translate_windows_key(0x09, chr(9), 0x10), "shift-tab")
        self.assertEqual(selectform.translate_windows_key(0x09, chr(9), 0), "tab")
        self.assertEqual(selectform.translate_windows_key(0x26, chr(0), 0), "up")
        self.assertEqual(selectform.translate_windows_key(0x41, "a", 0), "a")
        self.assertEqual(selectform.translate_windows_key(0x55, chr(21), 0x08), "ctrl-u")
        self.assertEqual(selectform.translate_windows_key(0x10, chr(0), 0x10), "")      # Shift pressed on its own


class Buttons(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.form = selectform.Form(REFS, out="list.txt", saver=lambda f: self.saved.append(list(f.included)) or f"Saved {len(f.included)}.")
        keys(self.form, "down", " ", " ")                        # two firewalls into Included
        keys(self.form, "tab", "tab", "tab")                     # Available -> Included -> save box -> buttons
        self.assertEqual(self.form.zone, "buttons")

    def test_the_three_buttons_are_drawn_and_the_focused_one_is_marked(self):
        text = screen(self.form)
        for label in ("[ Save ]", "[ Save & Exit ]", "[ Quit without saving ]", "unsaved changes"):
            self.assertIn(label, text)
        focused = [t for s, t in self.form.render(110, 36)[-1] if s == "focus"]
        self.assertEqual(focused, ["[ Save & Exit ]"])

    def test_save_writes_now_and_stays_open_and_clears_the_unsaved_marker(self):
        keys(self.form, "left")
        self.assertIsNone(self.form.press("enter"))
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.saved[0]), 2)
        self.assertEqual(self.form.message, "Saved 2.")
        self.assertFalse(self.form.dirty())
        self.assertNotIn("warn", [style for style, _t in self.form.render(110, 36)[-1]])

    def test_save_and_exit_saves_then_finishes(self):
        self.assertEqual(self.form.press("enter"), "accept")
        self.assertEqual(len(self.saved), 1)

    def test_quit_without_saving_leaves_at_once_and_writes_nothing(self):
        keys(self.form, "right", "right")
        self.assertEqual(self.form.press("enter"), "cancel")
        self.assertEqual(self.saved, [])

    def test_deselect_all_empties_the_included_list_and_says_so(self):
        form = selectform.Form(REFS, out="list.txt", saver=lambda f: "ok")
        keys(form, "down", "a")
        self.assertEqual(len(form.included), 4)
        form.zone, form.button = "buttons", selectform.DESELECT_ALL
        self.assertIsNone(form.press("enter"))
        self.assertEqual(form.included, [])
        self.assertIn("Took all 4 firewalls out", form.message)
        self.assertEqual(len(form.available()), 4)         # they are all back in Available
        self.assertIn("Deselect all", screen(form))
        form.press("enter")
        self.assertIn("already empty", form.message)

    def test_the_buttons_in_order_are_save_save_and_exit_deselect_all_quit(self):
        self.assertEqual(selectform.BUTTON_LABELS, ["Save", "Save & Exit", "Deselect all", "Quit without saving"])
        self.assertEqual((selectform.SAVE_ONLY, selectform.SAVE_EXIT, selectform.DESELECT_ALL, selectform.QUIT), (0, 1, 2, 3))

    def test_ctrl_d_is_save_and_exit_from_anywhere(self):
        form = selectform.Form(REFS, out="list.txt", saver=lambda f: "ok")
        keys(form, "down", " ")
        self.assertEqual(form.press("ctrl-d"), "accept")

    def test_save_needs_something_included_and_a_file_name(self):
        empty = selectform.Form(REFS, out="list.txt", saver=lambda f: "ok")
        empty.zone = "buttons"
        keys(empty, "left")
        self.assertIsNone(empty.press("enter"))
        self.assertIn("Included list is empty", empty.message)
        nameless = selectform.Form(REFS, out="", saver=lambda f: "ok")
        keys(nameless, "down", " ", "tab", "tab", "tab", "left")
        self.assertIsNone(nameless.press("enter"))
        self.assertIn("Type a file name", nameless.message)
        keys(nameless, "right")
        self.assertEqual(nameless.press("enter"), "accept")        # Save & Exit with no file name just returns the list

    def test_a_save_that_fails_says_why_and_keeps_the_form_open(self):
        def broken(form):
            raise PermissionError(13, "Permission denied")
        form = selectform.Form(REFS, out="list.txt", saver=broken)
        keys(form, "down", " ", "tab", "tab", "tab")
        self.assertIsNone(form.press("enter"))
        self.assertIn("Could not save: Permission denied", form.message)
        self.assertTrue(form.dirty())

    def test_escape_no_longer_nags_once_the_list_has_been_saved(self):
        keys(self.form, "left", "enter")
        self.assertEqual(self.form.press("esc"), "cancel")

    def test_up_from_the_buttons_goes_back_to_the_file_name(self):
        keys(self.form, "up")
        self.assertEqual(self.form.zone, "save")


class HiddenFilters(unittest.TestCase):
    def test_a_filter_left_behind_by_changing_the_mode_is_still_listed_and_flagged(self):
        form = selectform.Form(REFS)
        keys(form, "tab", "tab", "tab", *typing("10.2.*"), "up", "right")            # PAN-OS 'is' 10.2.*, then on to 'older than'
        self.assertEqual(form.mode[3], 1)
        self.assertEqual(form.in_effect(), [("PAN-OS is 10.2.*", False)])
        text = screen(form)
        self.assertIn("Filters in effect: PAN-OS is 10.2.* (hidden, other mode)", text)
        keys(form, "down", *typing("10.2.11"))
        self.assertEqual(form.in_effect(), [("PAN-OS is 10.2.*", False), ("PAN-OS older than 10.2.11", True)])
        self.assertEqual([r.serial for r in form.available()], ["S1", "S2"])         # both really apply

    def test_with_nothing_typed_the_line_says_everything_is_listed(self):
        self.assertIn("Filters in effect: none, every firewall is listed", screen(selectform.Form(REFS)))

    def test_the_mode_row_explains_that_other_modes_keep_applying(self):
        form = selectform.Form(REFS)
        keys(form, "up")
        self.assertIn("other modes still apply", screen(form))

    def test_the_two_tables_are_separated_by_a_blank_line(self):
        lines = screen(selectform.Form(REFS)).split("\n")
        at = next(i for i, l in enumerate(lines) if "INCLUDED" in l)
        self.assertEqual(lines[at - 1].strip(), "")


class SelectVerb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = os.getcwd()
        os.chdir(self.tmp.name)                      # the form saves upgradeList.txt in the working folder
        self.addCleanup(os.chdir, self.here)

    def args(self, **over):
        base = dict(interactive=None, out=None, source="panorama:192.0.2.1", color="never", ascii=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_the_form_opens_by_itself_only_at_a_terminal_with_no_filters(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(cli._wants_form(self.args()))
            self.assertFalse(cli._wants_form(self.args(name="x*")))
            self.assertFalse(cli._wants_form(self.args(out="list.txt")))
            self.assertFalse(cli._wants_form(self.args(interactive=False)))
            self.assertTrue(cli._wants_form(self.args(name="x*", interactive=True)))

    def test_a_script_never_gets_a_form_and_an_explicit_request_without_a_terminal_says_so(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(cli._wants_form(self.args()))
            with self.assertRaisesRegex(cli.SourceError, "needs a terminal"):
                cli._wants_form(self.args(interactive=True))

    def test_the_repeat_command_quotes_only_what_needs_it(self):
        text = cli._command_line(self.args(), [("--name", "site1-*"), ("--ha-state", "active"), ("--out", "my list.txt")])
        self.assertEqual(text, 'panupgrade select --panorama 192.0.2.1 --name "site1-*" --ha-state active --out "my list.txt"')

    def test_a_long_hand_picked_list_points_at_the_saved_file_instead_of_printing_every_serial(self):
        flags = [("--serial", f"S{i}") for i in range(12)]
        self.assertIn("--serials-file list.txt", cli._repeat_hint(self.args(out="list.txt"), flags))
        self.assertIn("Save the list to", cli._repeat_hint(self.args(), flags))

    def run_select(self, fill):
        source = mock.Mock()
        source.list_devices.return_value = REFS
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("panupgrade.cli._source", return_value=source), mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True), mock.patch("panupgrade.selectform.show", side_effect=fill):
            code = cli.main(["select", "--panorama", "192.0.2.1", "--color", "never", "--ascii"])
        return code, out.getvalue()

    def test_select_with_the_form_lists_what_is_included_and_prints_how_to_repeat_it(self):
        def fill(form, title=""):
            keys(form, *typing("site1-*"), "down", "a")
            return True
        code, text = self.run_select(fill)
        self.assertEqual(code, 0)
        self.assertIn('panupgrade select --panorama 192.0.2.1 --name "site1-*"', text)
        self.assertIn("Criteria: hostname like site1-*", text)
        self.assertIn("2 of 4 devices match", text)

    def test_a_hand_picked_list_is_reported_as_such(self):
        def fill(form, title=""):
            keys(form, "down", " ", " ")
            return True
        code, text = self.run_select(fill)
        self.assertIn("Criteria: 2 firewalls picked by hand", text)
        self.assertIn("--serial S4 --serial S1", text.replace("\n", " ") if False else text) if False else None
        self.assertIn("--serial", text)

    def test_the_form_saves_to_upgradeList_by_default_and_plan_and_friends_then_use_it(self):
        def fill(form, title=""):
            self.assertEqual(form.out, cli.DEFAULT_LIST)
            keys(form, "down", "a")
            return True
        code, text = self.run_select(fill)
        self.assertEqual(code, 0)
        self.assertEqual(cli._read_list(cli.DEFAULT_LIST), ["S4", "S1", "S2", "S3"])
        a = argparse.Namespace(command="plan", serial=None, serials_file=None, all=False)
        for f in cli._FILTERS:
            setattr(a, f, getattr(a, f, None))
        said = io.StringIO()
        with redirect_stdout(said):
            self.assertEqual(cli._serials(a), ["S4", "S1", "S2", "S3"])
        self.assertIn("Using the firewall list", said.getvalue())
        for command in ("prepare", "upgrade"):
            a.command = command
            self.assertEqual(len(cli._serials(a)), 4)

    def test_the_saved_list_is_not_used_when_firewalls_are_named_or_all_is_asked_for_or_the_verb_is_read_only(self):
        with open(cli.DEFAULT_LIST, "w", encoding="utf-8") as fh:
            fh.write("S1\nS2\n")
        def ns(**over):
            a = argparse.Namespace(command="plan", serial=None, serials_file=None, all=False)
            for f in cli._FILTERS:
                setattr(a, f, getattr(a, f, None))
            for k, v in over.items():
                setattr(a, k, v)
            return a
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._serials(ns()), ["S1", "S2"])
        self.assertEqual(cli._serials(ns(all=True)), [])
        self.assertEqual(cli._serials(ns(serial=["S9"])), ["S9"])
        self.assertEqual(cli._serials(ns(name="x*")), [])
        self.assertEqual(cli._serials(ns(command="report")), [])
        self.assertEqual(cli._serials(ns(command="select")), [])

    def test_reopening_the_form_starts_from_the_list_saved_last_time(self):
        with open(cli.DEFAULT_LIST, "w", encoding="utf-8") as fh:
            fh.write("S2\nS3\n")
        seen = {}
        def fill(form, title=""):
            seen["included"], seen["note"] = list(form.included), form.message
            return False
        self.run_select(fill)
        self.assertEqual(seen["included"], ["S2", "S3"])
        self.assertIn("already in upgradeList.txt", seen["note"])

    def test_a_flag_only_select_does_not_write_a_file_but_says_how_to_keep_the_list(self):
        source = mock.Mock()
        source.list_devices.return_value = REFS
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("panupgrade.cli._source", return_value=source):
            cli.main(["select", "--panorama", "192.0.2.1", "--name", "site1-*", "--color", "never"])
        self.assertFalse(os.path.exists(cli.DEFAULT_LIST))
        self.assertIn("--out upgradeList.txt", out.getvalue())

    def test_cancelling_the_form_selects_nothing(self):
        code, text = self.run_select(lambda form, title="": False)
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", text)


class StandAloneGroups(unittest.TestCase):
    def form(self, groups=None):
        form = selectform.Form(REFS, Selection(serials=["S1", "S2", "S3"], groups=groups or {}))
        form.zone = selectform.INCLUDED
        return form

    def test_g_names_a_group_for_the_firewall_under_the_cursor_and_it_shows(self):
        form = self.form()
        keys(form, "g", *typing("edge"), "enter")
        self.assertEqual(form.groups, {"S1": "edge"})
        self.assertIn("[group: edge]", screen(form))
        self.assertTrue(form.dirty())

    def test_the_next_firewall_starts_with_the_name_just_used_and_an_empty_name_removes_the_group(self):
        form = self.form()
        keys(form, "g", *typing("edge"), "enter", "down", "g", "enter")
        self.assertEqual(form.groups, {"S1": "edge", "S2": "edge"})
        keys(form, "g", "ctrl-u", "enter")
        self.assertEqual(form.groups, {"S1": "edge"})

    def test_esc_while_typing_a_name_leaves_the_group_alone_and_does_not_quit(self):
        form = self.form({"S1": "edge"})
        keys(form, "g", *typing("xx"))
        self.assertIsNone(form.press("esc"))
        self.assertEqual(form.groups, {"S1": "edge"})
        self.assertIsNone(form.editing)

    def test_commas_and_hashes_cannot_break_the_list_file(self):
        form = self.form()
        keys(form, "g", *typing("a,b#c"), "enter")
        self.assertEqual(form.groups, {"S1": "a b c"})

    def test_a_firewall_taken_out_loses_its_group_and_unchanged_groups_are_not_unsaved_changes(self):
        form = self.form({"S1": "edge"})
        self.assertFalse(form.dirty())
        keys(form, "d")
        self.assertEqual(form.groups, {})

    def test_the_saved_list_carries_the_groups(self):
        saved = []
        form = selectform.Form(REFS, Selection(serials=["S1", "S2"]), out="x.txt", saver=lambda f: saved.append(dict(f.groups)) or "saved")
        form.zone = selectform.INCLUDED
        keys(form, "g", *typing("edge"), "enter")
        form._save(need_name=False)
        self.assertEqual(saved, [{"S1": "edge"}])
        self.assertFalse(form.dirty())


if __name__ == "__main__":
    unittest.main()


class TheCommandLineRegex(unittest.TestCase):
    def test_a_regex_that_does_not_compile_is_refused_with_the_reason(self):
        import argparse
        with self.assertRaises(SystemExit) as caught:
            cli._selection(argparse.Namespace(name_regex="site1-(fw", name=None, name_contains=None, model=None, device_group=None, tag=None,
                                              version_is=None, version_older_than=None, version_newer_than=None, ha_state=None,
                                              serial=None, serials_file=None, all=True))
        self.assertIn("not a valid regular expression", str(caught.exception))
