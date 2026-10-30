# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The select form: tab between filter fields, see the matches update, get the equivalent command back."""

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


class FormBehaviour(unittest.TestCase):
    def setUp(self):
        self.form = selectform.Form(REFS)

    def test_it_starts_on_the_first_field_with_everything_matching(self):
        self.assertEqual(self.form.focus, 0)
        self.assertEqual(len(self.form.matching()), 4)

    def test_tab_and_shift_tab_move_between_fields_and_wrap(self):
        keys(self.form, "tab", "tab")
        self.assertEqual(self.form.focus, 2)
        keys(self.form, "shift-tab", "shift-tab", "shift-tab")
        self.assertEqual(self.form.focus, len(self.form.fields) - 1)
        keys(self.form, "tab")
        self.assertEqual(self.form.focus, 0)

    def test_typing_in_a_field_narrows_the_matches_as_you_go(self):
        keys(self.form, *typing("site1-*"))
        self.assertEqual([r.serial for r in self.form.matching()], ["S1", "S2"])
        keys(self.form, "backspace", "backspace")
        self.assertEqual(self.form.values["name_glob"], "site1")
        keys(self.form, "ctrl-u")
        self.assertEqual(len(self.form.matching()), 4)

    def test_fields_combine(self):
        keys(self.form, "tab", "tab", "tab", "tab", "tab", *typing("passive"))        # HA state is the sixth field
        self.assertEqual([r.serial for r in self.form.matching()], ["S2"])

    def test_a_bad_version_is_flagged_blocks_enter_and_does_not_break_the_preview(self):
        keys(self.form, "tab", "tab", "tab", *typing("banana"))
        self.assertIn("version_older_than", self.form.errors())
        self.assertIsNone(self.form.press("enter"))
        self.assertEqual(len(self.form.matching()), 4)
        keys(self.form, "ctrl-u", *typing("10.2.11"))
        self.assertEqual(self.form.errors(), {})
        self.assertEqual([r.serial for r in self.form.matching()], ["S1", "S2"])
        self.assertEqual(self.form.press("enter"), "accept")

    def test_escape_and_ctrl_c_cancel(self):
        self.assertEqual(selectform.Form(REFS).press("esc"), "cancel")
        self.assertEqual(selectform.Form(REFS).press("ctrl-c"), "cancel")

    def test_arrows_step_a_single_value_field_through_what_the_fleet_has(self):
        keys(self.form, "tab", "tab", "right")
        self.assertEqual(self.form.values["version_is"], "10.2.1")
        keys(self.form, "right", "right")
        self.assertEqual(self.form.values["version_is"], "12.1.10-h2")
        keys(self.form, "right")
        self.assertEqual(self.form.values["version_is"], "")          # round the end back to empty

    def test_arrows_add_and_remove_values_in_a_list_field(self):
        keys(self.form, *["tab"] * 5, "right", "right")
        self.assertEqual(self.form.values["ha_states"], "active, passive")
        keys(self.form, "right")                                      # nothing left to add
        self.assertEqual(self.form.values["ha_states"], "active, passive")
        keys(self.form, "left")
        self.assertEqual(self.form.values["ha_states"], "active")

    def test_the_fields_start_from_the_filters_already_given_on_the_command_line(self):
        form = selectform.Form(REFS, Selection(name_glob="site1-*", ha_states=["active", "passive"]), out="list.txt")
        self.assertEqual(form.values["name_glob"], "site1-*")
        self.assertEqual(form.values["ha_states"], "active, passive")
        self.assertEqual(form.values["out"], "list.txt")

    def test_the_equivalent_flags(self):
        form = selectform.Form(REFS, Selection(name_glob="site1-*", ha_states=["active", "passive"]), out="list.txt")
        self.assertEqual(form.flags(), [("--name", "site1-*"), ("--ha-state", "active"), ("--ha-state", "passive"), ("--out", "list.txt")])

    def test_the_selection_matches_what_the_flags_would_have_selected(self):
        keys(self.form, "tab", "tab", "tab", "tab", "tab", *typing("active"), "tab", *typing("PA-850"))
        sel = self.form.selection()
        self.assertEqual((sel.ha_states, sel.models), (["active"], ["PA-850"]))


class Drawing(unittest.TestCase):
    def test_the_screen_shows_the_fields_the_count_and_the_matching_firewalls(self):
        form = selectform.Form(REFS)
        text = "\n".join(t for _s, t in form.render(100, 40, "title here"))
        for needle in ("title here", "Hostname pattern", "Save the list to", "4 of 4 firewalls match", "site1-fw1", "gate01"):
            self.assertIn(needle, text)

    def test_a_short_window_gets_a_more_line_instead_of_overflowing(self):
        many = [ref(f"S{i}", f"fw{i}", "10.2.1") for i in range(60)]
        lines = selectform.Form(many).render(100, 30)
        self.assertLessEqual(len(lines), 30)
        self.assertIn("more", lines[-1][1])

    def test_no_line_is_wider_than_the_window(self):
        for _s, text in selectform.Form(REFS, Selection(name_glob="x" * 200)).render(60, 30):
            self.assertLess(len(text), 60)

    def test_the_focused_field_explains_itself(self):
        form = selectform.Form(REFS)
        self.assertIn("e.g. site1-*", "\n".join(t for _s, t in form.render(100, 40)))
        keys(form, *["tab"] * 5)
        self.assertIn("This fleet has: active, passive", "\n".join(t for _s, t in form.render(100, 40)))

    def test_run_feeds_keys_until_enter_and_cleans_up_nothing_it_did_not_start(self):
        presses = iter(typing("gate*") + ["enter"])
        out = io.StringIO()
        form = selectform.Form(REFS)
        self.assertTrue(selectform.run(form, lambda: next(presses), out, lambda: (100, 30)))
        self.assertEqual([r.serial for r in form.matching()], ["S4"])
        self.assertIn("\x1b[H", out.getvalue())

    def test_run_reports_a_cancel(self):
        self.assertFalse(selectform.run(selectform.Form(REFS), lambda: "esc", io.StringIO(), lambda: (100, 30)))


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

    def test_select_with_the_form_uses_what_was_filled_in_and_prints_how_to_repeat_it(self):
        def fill(form, title=""):
            form.values["name_glob"] = "site1-*"
            return True
        out = io.StringIO()
        source = mock.Mock()
        source.list_devices.return_value = REFS
        with redirect_stdout(out), mock.patch("panupgrade.cli._source", return_value=source), mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True), mock.patch("panupgrade.selectform.show", side_effect=fill):
            self.assertEqual(cli.main(["select", "--panorama", "192.0.2.1", "--color", "never", "--ascii"]), 0)
        text = out.getvalue()
        self.assertIn('panupgrade select --panorama 192.0.2.1 --name "site1-*"', text)
        self.assertIn("2 of 4 devices match", text)

    def test_cancelling_the_form_selects_nothing(self):
        source = mock.Mock()
        source.list_devices.return_value = REFS
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("panupgrade.cli._source", return_value=source), mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True), mock.patch("panupgrade.selectform.show", return_value=False):
            self.assertEqual(cli.main(["select", "--panorama", "192.0.2.1"]), 1)
        self.assertIn("Cancelled", out.getvalue())


if __name__ == "__main__":
    unittest.main()
