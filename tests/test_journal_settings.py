"""The journal's header carries the settings the plan was built with, so a finished run can be explained from its own log."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import sys  # noqa: F401  (puts src on the path)
from panupgrade import settings
from panupgrade.journal import Journal


class TheHeaderNamesTheSettings(unittest.TestCase):
    def test_every_setting_is_in_the_header_and_a_resume_still_works(self):
        s = settings.load(None, ["sync_to_peer=Disable", "sync_content=never"])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "run.jsonl"))
            Journal.create(path, "abc", s.__dict__.copy()).record("S1", "backup", "ok")
            header = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(header["settings"]["sync_to_peer"], "Disable")
            self.assertEqual(header["settings"]["sync_content"], "never")
            self.assertEqual(set(header["settings"]), set(s.__dict__))
            self.assertEqual(len(Journal.resume(path, "abc").events), 1)

    def test_a_journal_made_without_settings_has_a_plain_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "run.jsonl"))
            Journal.create(path, "abc")
            self.assertEqual(set(json.loads(Path(path).read_text(encoding="utf-8"))), {"ts", "type", "plan_hash"})
