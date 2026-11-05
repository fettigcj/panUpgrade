"""Guards that keep phase 1 honest: no way to reach a device, no secrets, no real-looking data."""

import ast
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.support import TARGET, TEST_RULES, demo_fleet_data, fleet, pair
from panupgrade import cli, report, settings
from panupgrade.planner import build_plan
from panupgrade.sources import FakeFleet

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IMPORTS = {"socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib", "telnetlib",
                     "subprocess", "xmlrpc", "asyncio", "panos", "pan", "paramiko", "lxml"}


class NoNetworkInPackage(unittest.TestCase):
    def test_package_imports_nothing_that_can_reach_a_device(self):
        for path in (ROOT / "src" / "panupgrade").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    self.assertNotIn(name.split(".")[0], FORBIDDEN_IMPORTS, f"{path.name} imports {name}")

    def test_planner_only_uses_the_read_interface(self):
        calls = []

        class Spy(FakeFleet):
            def __getattribute__(self, name):
                if not name.startswith("_") and name not in ("kind",):
                    calls.append(name)
                return super().__getattribute__(name)

        src = Spy({"panorama": {"version": "22.1.0"}, "devices": pair("A1", "A2")})
        build_plan(src, TARGET, settings.load(), rules=TEST_RULES)
        self.assertTrue(set(calls) <= {"list_devices", "facts", "panorama_version", "panorama_clock"}, set(calls))


class Hygiene(unittest.TestCase):
    def test_fixture_is_synthetic(self):
        text = (ROOT / "fixtures" / "sample-fleet.json").read_text(encoding="utf-8")
        self.assertFalse(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "IPv4-looking text in fixture")
        for major in re.findall(r'"(?:sw_version|version)": "(\d+)\.', text):
            self.assertGreaterEqual(int(major), 20, "fixture versions must be synthetic (major >= 20)")

    def test_fixture_matches_generator(self):
        on_disk = json.loads((ROOT / "fixtures" / "sample-fleet.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, json.loads(json.dumps(demo_fleet_data())))


class Reports(unittest.TestCase):
    def test_html_escapes_untrusted_names(self):
        d = pair("A1", "A2")
        d[0]["hostname"] = "<script>alert(1)</script>"
        html = report.to_html(build_plan(fleet(d), TARGET, settings.load(), rules=TEST_RULES))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_every_format_carries_the_report_only_banner(self):
        plan = build_plan(fleet(pair("A1", "A2")), TARGET, settings.load(), rules=TEST_RULES)
        for text in (report.to_json(plan), report.to_text(plan), report.to_html(plan)):
            self.assertIn("REPORT ONLY", text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    fixture = str(ROOT / "fixtures" / "sample-fleet.json")

    def test_plan_prints_text(self):
        code, out, _ = run(["plan", "--source", "fake:" + self.fixture, "--target", TARGET])
        self.assertEqual(code, 0)
        self.assertIn("REPORT ONLY", out)
        self.assertIn("SIMC0001", out)

    def test_plan_writes_files(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, _ = run(["plan", "--source", "fake:" + self.fixture, "--target", TARGET, "--out", d])
            self.assertEqual(code, 0)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["approval.template.json", "plan.html", "plan.json", "plan.txt"])
            self.assertEqual(json.loads(Path(d, "plan.json").read_text(encoding="utf-8"))["target"], TARGET)

    def test_a_live_source_with_unreadable_credentials_is_a_clean_error_and_never_reaches_the_network(self):
        code, _, err = run(["plan", "--panorama", "pano.example", "--target", TARGET, "--creds-file", "no-such-file.txt"])
        self.assertEqual(code, 2)
        self.assertIn("credentials file", err)

    def test_bad_input_is_exit_2_not_a_traceback(self):
        self.assertEqual(run(["plan", "--source", "fake:" + self.fixture, "--target", "banana"])[0], 2)
        self.assertEqual(run(["plan", "--source", "fake:" + self.fixture, "--target", TARGET,
                              "--set", "max_failures=abc"])[0], 2)
        self.assertEqual(run(["plan", "--source", "fake:missing.json", "--target", TARGET])[0], 2)

    def test_settings_command_shows_defaults_and_warns(self):
        code, out, err = run(["settings", "--set", "pending_changes=ignore"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["max_failures"], 5)
        self.assertIn("warn:", err)


if __name__ == "__main__":
    unittest.main()
