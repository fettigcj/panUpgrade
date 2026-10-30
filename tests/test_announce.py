# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The tool says what it is connecting to and who it will be, and never shows a password."""

import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import cli


def args(**over):
    base = dict(source="panorama:192.0.2.1", key_file=None, creds_file=None, account="panupgrade-read", work_account="panupgrade-write",
                color="never", ascii=True, settings=None, set=None)
    base.update(over)
    return argparse.Namespace(**base)


def said(fn, *a, **k):
    cli._SAID.clear()
    out = io.StringIO()
    with redirect_stdout(out):
        fn(*a, **k)
    return out.getvalue()


class Announce(unittest.TestCase):
    def test_says_panorama_credentials_file_and_account_with_their_origins(self):
        env = {"PANUPGRADE_CREDS": str(Path.home() / ".panupgrade" / "c.txt"), "PANUPGRADE_READ_ACCOUNT": "alice-read"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "DEFAULT_CREDS", env["PANUPGRADE_CREDS"]):
            text = said(cli._announce_connection, args(account="alice-read", creds_file=None))
        self.assertIn("Panorama: 192.0.2.1", text)
        self.assertIn(os.path.join("~", ".panupgrade", "c.txt"), text)          # under the home folder it is shown as ~
        self.assertIn("from PANUPGRADE_CREDS", text)
        self.assertIn("Reading as: alice-read (from PANUPGRADE_READ_ACCOUNT", text)
        self.assertNotIn("outside your home folder", text)

    def test_a_credentials_file_outside_home_is_called_out(self):
        text = said(cli._announce_connection, args(creds_file=os.path.abspath(os.sep + "shared" + os.sep + "creds.txt")))
        self.assertIn("outside your home folder", text)
        self.assertIn("never a shared file", text)

    def test_the_write_account_is_named_only_when_changes_will_be_made(self):
        self.assertNotIn("Changes will be made as", said(cli._announce_connection, args()))
        self.assertIn("Changes will be made as: panupgrade-write", said(cli._announce_connection, args(), writing=True))

    def test_each_line_appears_once_per_run(self):
        cli._SAID.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            cli._announce_connection(args())
            cli._announce_connection(args())
        self.assertEqual(out.getvalue().count("Panorama: 192.0.2.1"), 1)

    def test_a_fleet_file_is_described_as_a_simulation(self):
        self.assertIn("no real firewall is contacted", said(cli._announce_connection, args(source="fake:fleet.json")))

    def test_panorama_flag_is_documented_and_exclusive_with_source(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["help"]), 0)
        self.assertIn("--panorama <address>", out.getvalue())
        with self.assertRaises(SystemExit):                      # one of the two is required, never both
            cli.main(["report", "--panorama", "x", "--source", "fake:y"])


if __name__ == "__main__":
    unittest.main()
