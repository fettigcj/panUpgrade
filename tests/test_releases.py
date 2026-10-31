# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The table of PAN-OS releases: reading the vendor's page, counting minor versions, and the `releases` command."""

import argparse
import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import cli, kit, releases, settings
from panupgrade.versions import Version
from panupgrade_live import releasefeed

PAGE = """<html><body>
<table id="other"><tr><td>6.0.0 (incl.hotfixes)</td><td>May 2025</td></tr><tr><td>5.9</td><td>x</td><td>y</td></tr></table>
<table id="pan-os-panorama"><thead><tr><th colspan="5"><b>PAN-OS &amp; Panorama</b></th></tr>
<tr><th>Version</th><th>Release Date</th><th colspan="3">End-of-Life Date</th></tr></thead><tbody>
<tr><td>12.2</td><td>July 30th, 2026</td><td>July 30th, 2029</td><td>&nbsp;</td><td>July 30th, 2030</td></tr>
<tr><td>12.1</td><td>August 28, 2025</td><td>August 28, 2028</td><td>&nbsp;</td><td>August 28, 2029</td></tr>
<tr><td>11.2</td><td>May 2, 2024</td><td>May 2, 2027</td><td>&nbsp;</td><td>August 31, 2027</td></tr>
<tr><td>11.0</td><td>November 17, 2022</td><td>November 17, 2024</td><td>&nbsp;</td><td>&nbsp;</td></tr>
<tr><td>10.2</td><td>February 27, 2022</td><td>August 27, 2025</td><td>&nbsp;</td><td>March 31, 2027^</td></tr>
<tr><td>9.0-XFR (VM-Series only)</td><td>September 19, 2019</td><td>September 19, 2020</td><td>&nbsp;</td><td>&nbsp;</td></tr>
<tr><td>5.1 (Panorama only)</td><td>May 9, 2013</td><td>May 9, 2017</td><td>&nbsp;</td><td>&nbsp;</td></tr>
</tbody></table></body></html>"""


def table(*names):
    return releases.Releases([{"release": r} for r in names])


class ReadingThePage(unittest.TestCase):
    def test_only_the_pan_os_table_is_read_and_only_plain_releases_in_it(self):
        found = {e["release"]: e for e in releases.parse_eol_page(PAGE)}
        self.assertEqual(sorted(found, key=releases.key), ["10.2", "11.0", "11.2", "12.1", "12.2"])     # not 9.0-XFR, 5.1, 6.0 or 5.9
        self.assertEqual(found["12.2"], {"release": "12.2", "released": "2026-07-30", "standard_eol": "2029-07-30", "extended_eol": "2030-07-30"})
        self.assertEqual(found["11.0"]["extended_eol"], "")                       # a blank cell is no date
        self.assertEqual(found["10.2"]["extended_eol"], "2027-03-31")             # the footnote mark is ignored

    def test_a_page_that_no_longer_looks_right_is_refused_not_guessed_at(self):
        for page in ("<html></html>", PAGE.replace("pan-os-panorama", "renamed"), PAGE.replace("<td>10.2</td>", "<td>ten</td>")):
            with self.assertRaisesRegex(releases.ReleaseError, "no longer looks like"):
                releases.parse_eol_page(page)

    def test_download_stamps_the_date_and_keeps_the_source(self):
        fresh = releasefeed.download(datetime.date(2026, 10, 30), fetch=lambda: PAGE)
        self.assertEqual((fresh.fetched, fresh.source), ("2026-10-30", releases.URL))
        self.assertEqual(len(fresh.entries), 5)

    def test_a_failed_download_says_so(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            with self.assertRaisesRegex(releases.ReleaseError, "cannot download"):
                releasefeed.fetch_page()

    def test_the_download_checks_the_certificate(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"x")) as opened:
            opened.return_value.__enter__ = lambda s: s
            opened.return_value.__exit__ = lambda *a: None
            releasefeed.fetch_page()
        import ssl
        self.assertEqual(opened.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)


class TheBundledTable(unittest.TestCase):
    def test_it_loads_and_has_the_releases_the_lab_runs(self):
        bundled = releases.load()
        for release in ("10.2", "11.1", "11.2", "12.1", "12.2"):
            self.assertIn(tuple(map(int, release.split("."))), bundled.keys)
        self.assertNotIn((12, 0), bundled.keys)                                 # there is no 12.0
        self.assertTrue(bundled.fetched and bundled.source)

    def test_a_missing_or_broken_file_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(releases.ReleaseError, "no release table"):
                releases.load(Path(d, "none.json"))
            bad = Path(d, "bad.json")
            bad.write_text(json.dumps({"releases": [{"release": "twelve"}]}), encoding="utf-8")
            with self.assertRaisesRegex(releases.ReleaseError, "cannot read"):
                releases.load(bad)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            releases.save(Path(d, "r.json"), releases.load())
            self.assertEqual(releases.load(Path(d, "r.json")).entries, releases.load().entries)


class CountingMinorVersions(unittest.TestCase):
    def test_the_gap_where_12_0_never_was_is_not_counted(self):
        t = table("10.2", "11.0", "11.1", "11.2", "12.1", "12.2")
        behind = lambda p, f, unit="minor": t.behind(Version.parse(p), Version.parse(f), unit)
        self.assertEqual(behind("12.2.0", "12.1.0"), 1)
        self.assertEqual(behind("12.2.0", "11.2.0"), 2)
        self.assertEqual(behind("12.2.0", "11.1.0"), 3)
        self.assertEqual(behind("12.1.10-h2", "10.2.2"), 4)

    def test_major_counts_major_numbers_only(self):
        t = table("10.2", "11.0", "12.1")
        self.assertEqual(t.behind(Version.parse("12.1.10"), Version.parse("10.2.2"), "major"), 2)
        self.assertEqual(t.behind(Version.parse("12.1.10"), Version.parse("12.1.2"), "major"), 0)

    def test_a_release_the_table_does_not_list_yet_is_placed_by_its_numbers_and_reported(self):
        t = table("11.2", "12.1", "12.2")
        self.assertEqual(t.behind(Version.parse("13.1.0"), Version.parse("12.2.0"), "minor"), 1)
        self.assertEqual(t.behind(Version.parse("13.1.0"), Version.parse("11.2.0"), "minor"), 3)
        self.assertEqual(t.unlisted(Version.parse("13.1.0"), Version.parse("12.2.0")), ["13.1"])

    def test_nothing_is_behind_when_level_or_ahead(self):
        t = table("11.2", "12.1")
        self.assertEqual(t.behind(Version.parse("11.2.0"), Version.parse("12.1.0"), "minor"), 0)
        self.assertEqual(t.behind(Version.parse("11.2.0"), Version.parse("11.2.9"), "minor"), 0)

    def test_standing_follows_the_vendors_dates(self):
        entry = {"standard_eol": "2027-05-02", "extended_eol": "2027-08-31"}
        self.assertEqual(releases.standing(entry, datetime.date(2026, 10, 30)), "standard support")
        self.assertEqual(releases.standing(entry, datetime.date(2027, 6, 1)), "extended support")
        self.assertEqual(releases.standing(entry, datetime.date(2028, 1, 1)), "end of life")
        self.assertEqual(releases.standing({}, datetime.date(2028, 1, 1)), "unknown")


class SettingsChoices(unittest.TestCase):
    def test_the_new_names_are_the_choices_and_case_does_not_matter(self):
        self.assertEqual(settings.load().sync_to_peer, "Both")
        self.assertEqual(settings.load().panorama_compare, "minor")
        self.assertEqual(settings.load(None, ["sync_to_peer=upgradeonly"]).sync_to_peer, "UpgradeOnly")
        self.assertEqual(settings.load(None, ["sync_to_peer=DISABLE"]).sync_to_peer, "Disable")
        self.assertEqual(settings.load(None, ["panorama_compare=Major"]).panorama_compare, "major")

    def test_the_old_names_are_gone(self):
        for assignment in ("sync_to_peer=prepare", "sync_to_peer=upgrade", "sync_to_peer=never", "panorama_compare=feature", "panorama_compare=full"):
            with self.assertRaises(settings.SettingsError):
                settings.load(None, [assignment])

    def test_syncing_in_the_upgrade_run_is_the_default_so_it_is_no_longer_a_loosening(self):
        self.assertEqual(settings.load().loosening_warnings(), [])
        self.assertEqual(settings.load(None, ["sync_to_peer=UpgradeOnly"]).loosening_warnings(), [])


class TheReleasesCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.here)
        cli._SAID.clear()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["releases", "--color", "never", "--ascii", *argv])
        return code, out.getvalue()

    def test_it_shows_the_table_in_use_newest_first_with_where_it_came_from(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("from the table that ships with the tool (fetched", text)
        self.assertLess(text.index("12.2"), text.index("11.2"))
        self.assertIn("one step", text)

    def test_update_saves_here_and_says_what_is_new(self):
        old = releases.Releases([{"release": r} for r in ("10.2", "11.2", "12.1")], releases.URL, "2025-01-01")
        releases.save("releases.json", old)
        fresh = releases.Releases(releases.parse_eol_page(PAGE), releases.URL, "2026-10-30")
        with mock.patch("panupgrade_live.releasefeed.download", return_value=fresh):
            code, text = self.run_cli("--update")
        self.assertEqual(code, 0)
        self.assertIn("Saved 5 PAN-OS releases", text)
        self.assertIn("New since the table you had: 11.0, 12.2", text)
        self.assertEqual(len(releases.load("releases.json").entries), 5)

    def test_a_failed_update_leaves_the_file_alone(self):
        releases.save("releases.json", releases.load())
        before = Path("releases.json").read_text(encoding="utf-8")
        with mock.patch("panupgrade_live.releasefeed.download", side_effect=releases.ReleaseError("cannot download")):
            code, _ = self.run_cli("--update")
        self.assertEqual(code, 2)
        self.assertEqual(Path("releases.json").read_text(encoding="utf-8"), before)

    def test_an_old_table_is_called_old(self):
        releases.save("releases.json", releases.Releases([{"release": "12.1", "standard_eol": "2028-01-01"}], releases.URL, "2020-01-01"))
        _, text = self.run_cli()
        self.assertIn("more than six months old", text)

    def test_commands_use_a_table_found_in_the_folder_say_so_and_pin_it_for_the_upgrade_script(self):
        releases.save("releases.json", releases.load())
        args = argparse.Namespace(releases=None, color="never", ascii=True)
        out = io.StringIO()
        with redirect_stdout(out):
            cli._load_releases(args)
        self.assertIn("found in this folder", out.getvalue())
        self.assertEqual(Path(args.releases).name, "releases.json")
        full = argparse.Namespace(source="panorama:192.0.2.1", target="12.1.10", settings=None, creds_file="C:/c.txt", path_rules=None,
                                  releases=args.releases, account="a", work_account="a", ask_credentials=None, set=None)
        self.assertIn("--releases", kit.upgrade_argv(full, "s.txt", "a.json", "j.jsonl"))

    def test_without_a_file_the_shipped_table_is_used_and_said(self):
        args = argparse.Namespace(releases=None, color="never", ascii=True)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertTrue(cli._load_releases(args).entries)
        self.assertIn("release table that ships with the tool", out.getvalue())


if __name__ == "__main__":
    unittest.main()
