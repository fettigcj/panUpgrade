"""Old configuration backups: found by age and by the tool's own naming, offered for deletion once at start-up, never deleted unasked."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import sys  # noqa: F401  (puts src on the path)
from panupgrade import backups, settings


def make(folder, name, days_old, size=100):
    path = Path(folder, name)
    path.write_bytes(b"x" * size)
    stamp = time.time() - days_old * 86400
    os.utime(path, (stamp, stamp))
    return path


class FindingOldBackups(unittest.TestCase):
    def test_the_default_is_fourteen_days_and_zero_turns_it_off(self):
        self.assertEqual(settings.load(None, []).backup_keep_days, 14)
        self.assertEqual(settings.load(None, ["backup_keep_days=0"]).backup_keep_days, 0)
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["backup_keep_days=-1"])

    def test_only_the_tools_own_files_older_than_the_window_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = make(tmp, "fw-a_0123456789_20261101-101500.xml", 20)
            make(tmp, "fw-a_0123456789_20261120-101500.xml", 1)                    # recent
            make(tmp, "notes.txt", 90)                                              # not ours
            make(tmp, "my-own-backup.xml", 90)                                      # not ours
            (Path(tmp) / "sub").mkdir()
            self.assertEqual([p for p, _ in backups.old_backups(tmp, 14)], [old])

    def test_zero_days_and_a_missing_folder_find_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            make(tmp, "fw-a_0123456789_20261001-101500.xml", 90)
            self.assertEqual(backups.old_backups(tmp, 0), [])
            self.assertEqual(backups.old_backups(Path(tmp, "nope"), 14), [])

    def test_delete_removes_what_it_is_given_and_reports_what_it_could_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make(tmp, "fw-a_0123456789_20261001-101500.xml", 90)
            found = [(a, 100), (Path(tmp, "gone_0123456789_20261001-101500.xml"), 100)]
            gone, problems = backups.delete(found)
            self.assertEqual(gone, 1)
            self.assertEqual(len(problems), 1)
            self.assertFalse(a.exists())

    def test_the_question_says_how_many_and_how_big(self):
        text = backups.question([(Path("a"), 2048), (Path("b"), 2048)], 14, "/x/backups")
        self.assertIn("2 configuration backups", text)
        self.assertIn("older than 14 days (4 KB)", text)


class TheStartUpQuestionInTheShell(unittest.TestCase):
    def shell(self):
        from panupgrade import shell
        s = shell.Shell(shell.Session(None, "", "", None))
        return s

    def test_y_deletes_and_any_other_key_keeps_and_it_is_asked_only_once(self):
        for key, deleted in (("y", True), ("n", False), ("x", False)):
            with tempfile.TemporaryDirectory() as tmp:
                old = make(tmp, "fw-a_0123456789_20261001-101500.xml", 90)
                s = self.shell()
                s.offer_cleanup(backups.old_backups(tmp, 14), 14, tmp)
                self.assertIn("Delete it?", s.note)
                s.press(key)
                self.assertEqual(old.exists(), not deleted)
                self.assertIsNone(s.cleanup)
                self.assertEqual(s.note, "")
                s.press("x")                                                        # a later key is an ordinary key, not another answer
                self.assertEqual(old.exists(), not deleted)

    def test_nothing_old_means_no_question(self):
        s = self.shell()
        s.offer_cleanup([], 14, "x")
        self.assertIsNone(s.cleanup)


class TheQuestionOnTheCommandLine(unittest.TestCase):
    def ask(self, answer, tty=True):
        import argparse
        from unittest import mock
        from panupgrade import cli, console
        with tempfile.TemporaryDirectory() as tmp:
            old = make(tmp, "fw-a_0123456789_20261001-101500.xml", 90)
            cli._SAID.discard(("backups", tmp))
            s = settings.load(None, [f"backup_dir={tmp}"])
            style = console.Style(False, False)
            with mock.patch.object(cli.sys, "stdin") as stdin, mock.patch.object(cli.sys, "stdout") as out, mock.patch("builtins.input", return_value=answer) as asked:
                stdin.isatty.return_value = out.isatty.return_value = tty
                cli._offer_backup_cleanup(argparse.Namespace(), style, s)
                cli._offer_backup_cleanup(argparse.Namespace(), style, s)           # the second call in the same start asks nothing
            return old.exists(), asked.call_count

    def test_yes_deletes_anything_else_keeps_and_it_is_asked_once(self):
        self.assertEqual(self.ask("y"), (False, 1))
        self.assertEqual(self.ask(""), (True, 1))
        self.assertEqual(self.ask("n"), (True, 1))

    def test_without_a_terminal_nothing_is_asked_and_nothing_is_deleted(self):
        self.assertEqual(self.ask("y", tty=False), (True, 0))


if __name__ == "__main__":
    unittest.main()
