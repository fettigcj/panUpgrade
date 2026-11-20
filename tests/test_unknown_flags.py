"""A flag in the settings file that the tool does not recognize is ignored with a warning, and the settings form removes it when it saves."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.support import sys  # noqa: F401  (puts src on the path)
from panupgrade import cli, settings


class Base(unittest.TestCase):
    def file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name, "settings.toml")
        path.write_text(text, encoding="utf-8")
        return str(path)


class Loading(Base):
    def test_an_unrecognized_flag_is_skipped_and_named_and_the_rest_still_applies(self):
        ignored = []
        loaded = settings.load(self.file("max_parallel = 10\nmax_paralel = 3\nold_flag = true\n"), None, ignored)
        self.assertEqual(loaded.max_parallel, 10)
        self.assertEqual(ignored, ["max_paralel", "old_flag"])

    def test_without_asking_for_the_list_it_is_still_not_an_error(self):
        self.assertEqual(settings.load(self.file("old_flag = 1\n")).max_parallel, settings.Settings().max_parallel)

    def test_a_flag_typed_on_the_command_line_is_still_refused(self):
        with self.assertRaises(settings.SettingsError):
            settings.load(None, ["max_paralel=3"])
        with self.assertRaises(settings.SettingsError):
            settings.load(self.file("max_paralel = 3\n"), ["max_paralel=4"], [])

    def test_secrets_and_flags_that_changed_meaning_are_still_refused(self):
        for text in ("api_password = 'x'\n", "failover_band = 0.3\n"):
            with self.assertRaises(settings.SettingsError):
                settings.load(self.file(text))


class InTheSettingsForm(Base):
    def test_the_form_says_so_and_saving_removes_the_flag_and_keeps_the_rest(self):
        path = self.file("max_parallel = 10\nold_flag = true\n")
        args = cli.parse_args(["settings", "--settings", path])
        form = cli._settings_form(args)
        self.assertIn("old_flag", form.message)
        form._save()
        text = Path(path).read_text(encoding="utf-8")
        self.assertNotIn("old_flag", text)
        self.assertIn("max_parallel = 10", text)

    def test_a_command_says_it_once(self):
        path = self.file("old_flag = true\n")
        args = cli.parse_args(["settings", "--settings", path, "--color", "never"])
        out = io.StringIO()
        with redirect_stdout(out):
            cli._load_settings(args)
        self.assertIn("unrecognized flag 'old_flag' in the settings file is being ignored", out.getvalue())


if __name__ == "__main__":
    unittest.main()
