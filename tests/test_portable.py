"""The repository is a product, not one site's workspace: nothing in it may name a particular lab, host, person, path or credential."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Built from pieces so this file does not trip its own scan.
SITE_SPECIFIC = [
    r"172\.31\.",                                  # a private address range used by one lab
    r"pano" + r"01", r"pavm" + r"102",             # one lab's Panorama and firewall names
    r"panUpgrade(Prep|Work)",                      # one lab's account names
    r"fet" + r"ti", r"claude_Lab" + r"Management", r"\.sec" + r"rets",
    r"\b0079[0-9]{9,}\b",                          # a real-looking serial
    r"[A-Z]:[/\\](Users|var)[/\\]",                # an absolute Windows path
    r"\bHV0[0-9]\b",
]
SKIP_DIRS = {".git", "__pycache__", ".venv", "out", "runs", "prepared", "backups", "panupgrade-data"}


class NothingSiteSpecific(unittest.TestCase):
    def test_no_site_specific_text_in_the_repository(self):
        pattern = re.compile("|".join(SITE_SPECIFIC))
        found = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts) or path.suffix in (".pyc", ".xlsx"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(found, [], "site-specific text belongs in the site's own working folder, not in the repository")


if __name__ == "__main__":
    unittest.main()
