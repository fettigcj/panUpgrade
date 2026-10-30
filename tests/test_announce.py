# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The tool follows however the site signs in, says what it is doing, and never shows a password."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import cli, kit
from panupgrade.sources import SourceError


def args(**over):
    base = dict(source="panorama:192.0.2.1", key_file=None, creds_file=None, account=None, work_account=None, ask_credentials=None,
                color="never", ascii=True, settings=None, set=None, target="10.2.2", path_rules=None)
    base.update(over)
    return argparse.Namespace(**base)


def said(fn, *a, **k):
    cli._SAID.clear()
    out = io.StringIO()
    with redirect_stdout(out):
        fn(*a, **k)
    return out.getvalue()


class SignIn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def creds(self, text):
        path = Path(self.tmp.name, "creds.txt")
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_file_with_one_account_needs_no_account_flag(self):
        a = args(creds_file=self.creds("{'tool': 'x'}"))
        text = said(cli._announce_connection, a)
        self.assertIn("Reading as: tool (the only account in that file)", text)
        self.assertEqual(cli._login(a), {"reading": "tool", "changes": "tool", "ask": None, "how": "the only account in that file"})

    def test_read_only_verbs_have_no_work_account_option_and_still_sign_in(self):
        a = args(creds_file=self.creds("{'tool': 'x'}"))
        del a.work_account
        self.assertEqual(cli._login(a)["changes"], "tool")

    def test_several_accounts_and_none_named_asks_which_without_guessing(self):
        a = args(creds_file=self.creds("{'a': 'x', 'b': 'y'}"))
        with self.assertRaisesRegex(SourceError, "holds 2 accounts \\(a, b\\).*--account NAME.*--ask-credentials"):
            cli._login(a)

    def test_one_account_for_everything_is_the_default_and_a_separate_one_is_optional(self):
        same = args(creds_file=self.creds("{'a': 'x', 'b': 'y'}"), account="a")
        self.assertIn("Changes will be made as: a (the same account)", said(cli._announce_connection, same, writing=True))
        split = args(creds_file=self.creds("{'a': 'x', 'b': 'y'}"), account="a", work_account="b")
        text = said(cli._announce_connection, split, writing=True)
        self.assertIn("Reading as: a", text)
        self.assertIn("Changes will be made as: b", text)

    def test_typing_a_login_for_changes_only(self):
        a = args(creds_file=self.creds("{'tool': 'x'}"), ask_credentials="changes")
        text = said(cli._announce_connection, a, writing=True)
        self.assertIn("Reading as: tool", text)
        self.assertIn("Changes will be made with a login you type at the prompt", text)

    def test_typing_a_login_for_everything_needs_no_file_at_all(self):
        a = args(ask_credentials="all", creds_file=os.path.join(self.tmp.name, "does-not-exist"))
        text = said(cli._announce_connection, a)
        self.assertIn("You will be asked for a username and password", text)
        self.assertNotIn("Credentials file", text)

    def test_a_typed_login_needs_a_terminal_and_never_hangs_a_script(self):
        a = args(ask_credentials="all")
        with mock.patch("sys.stdin.isatty", return_value=False), self.assertRaisesRegex(SourceError, "needs a terminal"):
            cli._typed(a, "192.0.2.1")

    def test_the_path_is_shown_under_the_home_folder_and_no_password_is_ever_printed(self):
        path = str(Path.home() / ".panupgrade" / "c.txt")
        with mock.patch("panupgrade_live.panorama.load_accounts", return_value={"tool": "hunter2"}):
            text = said(cli._announce_connection, args(creds_file=path))
        self.assertIn(os.path.join("~", ".panupgrade", "c.txt"), text)
        self.assertNotIn("hunter2", text)

    def test_each_line_appears_once_per_run(self):
        cli._SAID.clear()
        out = io.StringIO()
        a = args(creds_file=self.creds("{'tool': 'x'}"))
        with redirect_stdout(out):
            cli._announce_connection(a)
            cli._announce_connection(a)
        self.assertEqual(out.getvalue().count("Panorama: 192.0.2.1"), 1)

    def test_a_fleet_file_is_described_as_a_simulation(self):
        self.assertIn("no real firewall is contacted", said(cli._announce_connection, args(source="fake:fleet.json")))

    def test_panorama_flag_is_documented_and_exclusive_with_source(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["help"]), 0)
        self.assertIn("--panorama <address>", out.getvalue())
        with self.assertRaises(SystemExit):
            cli.main(["report", "--panorama", "x", "--source", "fake:y"])


class KitCarriesTheSignInChoice(unittest.TestCase):
    def argv(self, **over):
        a = args(creds_file="C:/creds.txt", **over)
        return kit.upgrade_argv(a, "s.txt", "a.json", "j.jsonl")

    def test_separate_accounts_are_pinned(self):
        a = args(creds_file="C:/creds.txt")
        a._login = {"reading": "tool", "changes": "admin", "ask": None, "how": ""}
        argv = kit.upgrade_argv(a, "s.txt", "a.json", "j.jsonl")
        self.assertIn("--account", argv)
        self.assertEqual(argv[argv.index("--work-account") + 1], "admin")

    def test_one_account_for_everything_adds_no_work_account(self):
        a = args(creds_file="C:/creds.txt")
        a._login = {"reading": "tool", "changes": "tool", "ask": None, "how": ""}
        self.assertNotIn("--work-account", kit.upgrade_argv(a, "s.txt", "a.json", "j.jsonl"))

    def test_a_typed_login_makes_the_script_ask_again_when_it_runs(self):
        a = args(creds_file="C:/creds.txt", ask_credentials="changes")
        a._login = {"reading": "tool", "changes": None, "ask": "changes", "how": ""}
        argv = kit.upgrade_argv(a, "s.txt", "a.json", "j.jsonl")
        self.assertEqual(argv[argv.index("--ask-credentials") + 1], "changes")
        self.assertNotIn("--work-account", argv)


if __name__ == "__main__":
    unittest.main()
