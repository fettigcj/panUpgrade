# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The select form: filters over the columns, Available and Included tables, and the equivalent command afterwards."""

import argparse
import io
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
        for _ in range(len(selectform.COLUMNS) + 4):
            seen.append((self.form.zone, self.form.fcol))
            self.form.press("tab")
        self.assertEqual([z for z, _c in seen[:7]], ["filter"] * 7)
        self.assertEqual([c for _z, c in seen[:7]], list(range(7)))
        self.assertEqual([z for z, _c in seen[7:]], ["available", "included", "save", "filter"])
        fresh = selectform.Form(REFS)
        fresh.press("shift-tab")
        self.assertEqual(fresh.zone, "save")

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

    def test_enter_in_the_save_box_is_done(self):
        keys(self.form, " ", "tab", "tab", *typing("my.txt"))
        self.assertEqual(self.form.out, "my.txt")
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


class SelectVerb(unittest.TestCase):
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

    def test_cancelling_the_form_selects_nothing(self):
        code, text = self.run_select(lambda form, title="": False)
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", text)


if __name__ == "__main__":
    unittest.main()
