# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The sign-in screen: accounts in the credentials file, the choice of account, and what happens when credentials are missing."""

import argparse
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import authform, cli, credstore, site, tui
from panupgrade.sources import SourceError

SECRET = "SECRET-VALUE-123"
KEYS = ("PANUPGRADE_HOME", "PANUPGRADE_CREDS", "PANUPGRADE_ACCOUNT", "PANUPGRADE_WORK_ACCOUNT")


class Home(unittest.TestCase):
    """Every test gets its own sign-in folder, and none of the real PANUPGRADE_* variables."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in KEYS}
        env["PANUPGRADE_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cli._SAID.clear()
        cli._KEYS.clear()

    def creds(self, text):
        path = self.home / "credentials.txt"
        path.write_text(text, encoding="utf-8")
        return path


class SettingsPrecedence(Home):
    def test_defaults_when_nothing_is_set(self):
        self.assertEqual(site.creds_path(), (str(self.home / "credentials.txt"), "default"))
        self.assertEqual(site.account(), (None, "default"))

    def test_the_settings_file_beats_the_default_and_an_environment_variable_beats_the_file(self):
        (self.home / "auth.json").write_text(json.dumps({"account": "from-file", "work_account": "worker"}), encoding="utf-8")
        self.assertEqual(site.account(), ("from-file", "auth.json"))
        self.assertEqual(site.work_account(), ("worker", "auth.json"))
        os.environ["PANUPGRADE_ACCOUNT"] = "from-env"
        self.assertEqual(site.account(), ("from-env", "PANUPGRADE_ACCOUNT"))

    def test_a_broken_settings_file_is_just_no_settings(self):
        (self.home / "auth.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(site.account(), (None, "default"))

    def test_the_announcement_says_where_the_choice_came_from(self):
        (self.home / "auth.json").write_text(json.dumps({"account": "svc"}), encoding="utf-8")
        creds = self.creds("{'svc': 'pw', 'other': 'pw'}")
        a = argparse.Namespace(source="panorama:192.0.2.1", creds_file=None, account=None, work_account=None, ask_credentials=None,
                               color="never", ascii=True)
        out = io.StringIO()
        with redirect_stdout(out):
            cli._announce_connection(a)
        text = out.getvalue()
        self.assertIn("Reading as: svc (from your sign-in settings", text)
        self.assertIn("default location", text)
        self.assertNotIn("pw", text.replace("PANUPGRADE", ""))
        self.assertTrue(creds.is_file())


class TheCredentialsFile(Home):
    def test_accounts_round_trip_and_the_file_stays_readable_by_hand(self):
        accounts = {"person": "pa'ss\"word", "service": {"api_key": "KEY"}}
        credstore.write(self.home / "sub" / "c.txt", accounts)                          # the folder is created
        self.assertEqual(credstore.read(self.home / "sub" / "c.txt"), accounts)
        self.assertEqual((self.home / "sub" / "c.txt").read_text(encoding="utf-8").splitlines()[0], "{")

    @unittest.skipIf(os.name == "nt", "file modes are not how Windows says who may read a file")
    def test_a_new_file_is_for_its_owner_alone(self):
        credstore.write(self.home / "c.txt", {"a": "b"})
        self.assertEqual(stat.S_IMODE((self.home / "c.txt").stat().st_mode), 0o600)

    def test_missing_is_something_to_fix_and_malformed_is_not_overwritten(self):
        with self.assertRaises(credstore.NeedsSignIn):
            credstore.read(self.home / "none.txt")
        bad = self.creds("{'a': 5}")
        with self.assertRaises(SourceError) as caught:
            credstore.read(bad)
        self.assertNotIsInstance(caught.exception, credstore.NeedsSignIn)

    def test_refuses_to_write_nonsense(self):
        for accounts in ({"": "pw"}, {"a": ""}, {"a": {"api_key": ""}}, {"a": 5}):
            with self.assertRaises(ValueError):
                credstore.write(self.home / "c.txt", accounts)
        self.assertFalse((self.home / "c.txt").exists())


class FakeServices:
    def __init__(self):
        self.saved, self.tested, self.signed_in = [], [], []

    def sign_in(self, address, user, password):
        self.signed_in.append((address, user))
        if password == "wrong":
            raise SourceError(f"{address}: login as {user} failed")
        return "KEY-FROM-LOGIN"

    def test(self, address, name, secret):
        self.tested.append((address, name))
        return True, f"{name} signed in to {address}."

    def save(self, form):
        self.saved.append(dict(form.accounts))
        return authform.save_all(form, set_user_env=lambda *a: None, delete_user_env=lambda *a: None)


def typing(text):
    return list(text)


def keys(form, *presses):
    for p in presses:
        form.press(p)


def screen(form, width=110, height=36):
    return tui.plain(form.render(width, height, "title here"))


class TheForm(Home):
    def form(self, accounts=None, **over):
        path = self.home / "credentials.txt"
        values = dict(reading="", changes="", store="config", address="", notice="", services=FakeServices(), overrides=())
        values.update(over)
        return authform.Form(accounts if accounts is not None else {}, path, "default location", **values)

    def add(self, form, name, kind, secret):
        form.zone = "accounts"
        keys(form, "a", *typing(name), "tab")
        for _ in range(kind):
            form.press("right")
        keys(form, "tab", *typing(secret), "tab", "enter")

    def test_a_password_account_is_added_and_never_shown_again(self):
        form = self.form()
        self.add(form, "person", 0, SECRET)
        self.assertEqual(form.accounts, {"person": SECRET})
        self.assertIn("Added person", form.message)
        text = screen(form)
        self.assertIn("person", text)
        self.assertIn("password", text)
        self.assertNotIn(SECRET, text)

    def test_the_secret_is_masked_while_it_is_typed(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "a", *typing("person"), "tab", "tab", *typing(SECRET))
        text = screen(form)
        self.assertIn("*" * len(SECRET), text)
        self.assertNotIn(SECRET, text)

    def test_an_api_key_account_stores_the_key(self):
        form = self.form()
        self.add(form, "service", 1, " KEY-1 ")
        self.assertEqual(form.accounts, {"service": {"api_key": "KEY-1"}})
        self.assertIn("API key", screen(form))

    def test_sign_in_now_keeps_only_the_key_and_forgets_the_password(self):
        form = self.form(address="192.0.2.1")
        self.add(form, "admin", 2, "their-password")
        self.assertEqual(form.accounts, {"admin": {"api_key": "KEY-FROM-LOGIN"}})
        self.assertEqual(form.services.signed_in, [("192.0.2.1", "admin")])
        self.assertNotIn("their-password", repr(form.accounts))

    def test_a_failed_sign_in_keeps_the_dialog_open_and_adds_nothing(self):
        form = self.form(address="192.0.2.1")
        self.add(form, "admin", 2, "wrong")
        self.assertEqual(form.accounts, {})
        self.assertIsNotNone(form.modal)
        self.assertIn("login as admin failed", form.message)

    def test_sign_in_now_needs_the_panorama_address(self):
        form = self.form()
        self.add(form, "admin", 2, "pw")
        self.assertEqual(form.accounts, {})
        self.assertIn("needs the Panorama address", form.message)

    def test_a_name_and_a_secret_are_required_and_escape_closes_the_dialog(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "a", "tab", "tab", "tab", "enter")                 # name, kind, secret, then the Add button
        self.assertIn("needs a name", form.message)
        self.assertIsNotNone(form.modal)
        keys(form, *typing("x"), "tab", "tab", "tab", "enter")
        self.assertIn("Type the password", form.message)
        form.press("esc")
        self.assertIsNone(form.modal)

    def test_remove_and_the_choices_for_reading_and_changes(self):
        form = self.form({"a": "1", "b": "2", "c": "3"})
        form.zone = "accounts"
        keys(form, "r")                                              # a for reading
        keys(form, "down", "c")                                      # b for changes
        self.assertEqual((form.reading, form.changes), ("a", "b"))
        keys(form, "c")                                              # again: back to the same as reading
        self.assertEqual(form.changes, "")
        keys(form, "c", "delete")                                    # b chosen for changes, then removed
        self.assertEqual((form.names(), form.reading, form.changes), (["a", "c"], "a", ""))
        self.assertIn("used for", screen(form))

    def test_the_only_account_is_used_for_everything_without_choosing(self):
        form = self.form({"solo": "pw"})
        self.assertEqual((form.effective_reading(), form.effective_changes()), ("solo", "solo"))
        self.assertIn("only account", screen(form))

    def test_test_sign_in_needs_an_address_then_reports(self):
        form = self.form({"a": "pw"})
        form.zone = "accounts"
        keys(form, "t")
        self.assertIn("Type the Panorama address", form.message)
        self.assertEqual(form.zone, "address")
        keys(form, *typing("192.0.2.1"), "tab", "tab", "t")
        self.assertEqual(form.services.tested, [("192.0.2.1", "a")])
        self.assertEqual(form.message, "a signed in to 192.0.2.1.")

    def test_the_ring_of_zones_wraps(self):
        form = self.form({"a": "pw"})
        form.zone = "address"
        seen = []
        for _ in range(6):
            seen.append(form.zone)
            form.press("tab")
        self.assertEqual(seen, ["address", "store", "accounts", "actions", "buttons", "address"])
        form.zone = "address"
        form.press("shift-tab")
        self.assertEqual(form.zone, "buttons")

    def test_unsaved_changes_are_marked_and_escape_asks_twice(self):
        form = self.form({"a": "pw"})
        self.assertEqual(form.press("esc"), "cancel")
        form.zone = "accounts"
        keys(form, "r")
        self.assertTrue(form.dirty())
        self.assertIn("unsaved changes", screen(form))
        self.assertIsNone(form.press("esc"))
        self.assertEqual(form.press("esc"), "cancel")

    def test_no_line_is_wider_than_the_window_and_the_buttons_stay_visible(self):
        form = self.form({f"account-{i}": "pw" for i in range(12)}, address="192.0.2.1")
        for width, height in ((60, 20), (90, 24), (140, 40)):
            lines = form.render(width, height)
            self.assertLessEqual(len(lines), height)
            for line in lines:
                self.assertLess(len("".join(t for _s, t in line)), width)
            self.assertIn("[ Save & Exit ]", tui.plain(lines))


class Saving(Home):
    def form(self, accounts, **over):
        values = dict(reading="", changes="", store="config", services=FakeServices())
        values.update(over)
        return authform.Form(accounts, self.home / "credentials.txt", "default location", **values)

    def test_save_writes_the_file_and_the_settings_and_nothing_secret_goes_in_the_settings(self):
        form = self.form({})
        form.accounts.update({"a": SECRET, "b": {"api_key": "KEY"}})
        form.reading, form.changes = "a", "b"
        message = authform.save_all(form, set_user_env=mock.Mock(), delete_user_env=mock.Mock())
        self.assertIn("2 accounts", message)
        self.assertEqual(credstore.read(self.home / "credentials.txt"), {"a": SECRET, "b": {"api_key": "KEY"}})
        config = json.loads((self.home / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"account": "a", "work_account": "b"})
        self.assertNotIn(SECRET, (self.home / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual((site.account()[0], site.work_account()[0]), ("a", "b"))

    def test_clearing_the_choices_removes_them_and_an_empty_settings_file_goes_away(self):
        (self.home / "auth.json").write_text(json.dumps({"account": "a"}), encoding="utf-8")
        credstore.write(self.home / "credentials.txt", {"a": "pw"})
        form = self.form({"a": "pw"})
        authform.save_all(form, set_user_env=mock.Mock(), delete_user_env=mock.Mock())
        self.assertFalse((self.home / "auth.json").exists())

    def test_the_environment_choice_sets_user_variables_and_leaves_the_settings_file_without_them(self):
        (self.home / "auth.json").write_text(json.dumps({"account": "old", "creds_file": "keep.txt"}), encoding="utf-8")
        form = self.form({"a": "pw", "b": "pw"}, reading="a", changes="", store="env")
        set_var, delete_var = mock.Mock(), mock.Mock()
        message = authform.save_all(form, set_user_env=set_var, delete_user_env=delete_var)
        set_var.assert_called_once_with("PANUPGRADE_ACCOUNT", "a")
        delete_var.assert_called_once_with("PANUPGRADE_WORK_ACCOUNT")
        self.assertEqual(json.loads((self.home / "auth.json").read_text(encoding="utf-8")), {"creds_file": "keep.txt"})
        self.assertIn("new terminal", message)

    def test_switching_back_to_the_settings_file_clears_the_user_variables_that_would_override_it(self):
        form = self.form({"a": "pw"}, reading="a", store="config")
        deleted = []
        with mock.patch("panupgrade.userenv.supported", return_value=True), mock.patch("panupgrade.userenv.get", return_value="a"):
            authform.save_all(form, set_user_env=mock.Mock(), delete_user_env=deleted.append)
        self.assertEqual(sorted(deleted), ["PANUPGRADE_ACCOUNT", "PANUPGRADE_WORK_ACCOUNT"])

    def test_the_form_saves_through_its_services_and_marks_itself_clean(self):
        form = self.form({"a": "pw"})
        form.zone = "accounts"
        keys(form, "r", "tab", "tab")                                 # accounts -> actions -> buttons
        self.assertEqual(form.zone, "buttons")
        self.assertEqual(form.press("enter"), "accept")               # Save & Exit
        self.assertFalse(form.dirty())
        self.assertTrue(form.ever_saved)
        self.assertEqual(site.account()[0], "a")

    def test_a_failed_save_says_why_and_stays_open(self):
        class Broken(FakeServices):
            def save(self, form):
                raise PermissionError(13, "Permission denied")
        form = self.form({"a": "pw"}, services=Broken())
        form.zone = "accounts"
        keys(form, "r")
        self.assertIsNone(form.press("ctrl-d"))
        self.assertIn("Could not save: Permission denied", form.message)
        self.assertTrue(form.dirty())

    def test_environment_variables_are_not_offered_where_there_are_none(self):
        form = self.form({"a": "pw"})
        form.zone = "store"
        with mock.patch("panupgrade.userenv.supported", return_value=False):
            form.press("right")
        self.assertEqual(form.store, "config")
        self.assertIn("no user-level environment variables", form.message)


class WhenCredentialsAreMissing(Home):
    def args(self, **over):
        base = dict(source="panorama:192.0.2.1", creds_file=None, account=None, work_account=None, ask_credentials=None, color="never", ascii=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_on_a_terminal_the_sign_in_screen_opens_and_the_command_carries_on_once_it_has_saved(self):
        def fix(args):
            credstore.write(self.home / "credentials.txt", {"tool": "pw"})
            return True
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", side_effect=fix) as screen_:
            login = cli._login(self.args())
        self.assertEqual(login["reading"], "tool")
        screen_.assert_called_once()
        self.assertIn("Cannot sign in yet: there is no credentials file at", out.getvalue())

    def test_quitting_the_screen_without_saving_leaves_the_error(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", return_value=False):
            with self.assertRaises(credstore.NeedsSignIn):
                cli._login(self.args())

    def test_a_script_gets_the_error_and_no_screen(self):
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("panupgrade.cli._run_auth_screen") as screen_:
            with self.assertRaisesRegex(credstore.NeedsSignIn, "panupgrade auth"):
                cli._login(self.args())
        screen_.assert_not_called()

    def test_several_accounts_and_none_chosen_also_opens_the_screen_and_it_is_offered_only_once(self):
        self.creds("{'a': 'x', 'b': 'y'}")
        calls = []
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", side_effect=lambda a: calls.append(1) or False):
            for _ in range(2):
                with self.assertRaisesRegex(credstore.NeedsSignIn, "none is chosen"):
                    cli._login(self.args())
        self.assertEqual(len(calls), 1)

    def test_an_account_missing_from_the_file_is_offered_the_screen_too(self):
        self.creds("{'a': 'x'}")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", return_value=False) as screen_:
            with self.assertRaises(credstore.NeedsSignIn):
                cli._key_for(self.args(account="ghost"), "192.0.2.1", "reading")
        screen_.assert_called_once()


class TheAuthCommand(Home):
    def test_without_a_terminal_it_prints_a_summary_with_no_secrets(self):
        self.creds("{'person': '" + SECRET + "', 'service': {'api_key': 'KEY-VALUE'}}")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["auth", "--color", "never", "--ascii"]), 0)
        text = out.getvalue()
        self.assertIn("account person: password", text)
        self.assertIn("account service: API key", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("KEY-VALUE", text)

    def test_the_summary_does_not_claim_an_account_is_chosen_when_none_is(self):
        self.creds("{'a': 'x', 'b': 'y'}")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            cli.main(["auth", "--color", "never", "--ascii"])
        self.assertIn("none chosen yet", out.getvalue())
        self.assertNotIn("only account", out.getvalue())
        os.environ["PANUPGRADE_ACCOUNT"] = "a"
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            cli.main(["auth", "--color", "never", "--ascii"])
        self.assertIn("reading account: a (from PANUPGRADE_ACCOUNT)", out.getvalue())

    def test_without_a_file_it_says_how_to_make_one(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["auth", "--color", "never", "--ascii"]), 0)
        self.assertIn("no credentials file there yet", out.getvalue())

    def test_on_a_terminal_it_opens_the_screen_with_the_panorama_address_filled_in(self):
        seen = {}

        def fake_show(form, title=""):
            seen["address"], seen["accounts"] = form.address, dict(form.accounts)
            return False
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=fake_show):
            code = cli.main(["auth", "--panorama", "192.0.2.1", "--color", "never", "--ascii"])
        self.assertEqual(code, 1)
        self.assertEqual(seen, {"address": "192.0.2.1", "accounts": {}})

    def test_a_credentials_file_it_cannot_read_is_never_overwritten(self):
        bad = self.creds("{'a': 5}")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show") as shown:
            cli.main(["auth", "--color", "never", "--ascii"])
        shown.assert_not_called()
        self.assertIn("will not overwrite", " ".join(out.getvalue().split()))
        self.assertEqual(bad.read_text(encoding="utf-8"), "{'a': 5}")


if __name__ == "__main__":
    unittest.main()
