"""The workbook writer and the read-only Panorama transport (no network is used here)."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from tests.support import TARGET, TEST_RULES, demo_fleet_data
from panupgrade import settings, xlsxout
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet
from panupgrade_live.panorama import PanoramaApi, to_xml_cmd

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _rows(path, sheet):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    return [[(c.find("m:is/m:t", NS).text or "") for c in row] for row in root.iter("{%s}row" % NS["m"])]


class Workbook(unittest.TestCase):
    def test_firewalls_sheet_keeps_the_legacy_columns_and_one_row_per_device(self):
        plan = build_plan(FakeFleet(demo_fleet_data()), TARGET, settings.load(None, None), Selection(), TEST_RULES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.xlsx"
            xlsxout.write_workbook(path, plan)
            rows = _rows(path, 1)
            checks = _rows(path, 2)
        self.assertEqual(rows[0], ["serial", "hostname", "haState", "upgradeable", "details", "startingVersion", "endingVersion"])
        self.assertEqual(len(rows) - 1, len(plan["devices"]))
        self.assertEqual(checks[0][:4], ["serial", "hostname", "check", "status"])
        self.assertTrue(all(r[3] in ("True", "False") for r in rows[1:]))

    def test_cell_text_is_escaped(self):
        plan = {"devices": [{"serial": "S<1>", "hostname": "a&b", "ha_state": "", "verdict": "blocked",
                             "current_version": "1.0.0", "results": [{"check": "c", "status": "fail", "reason": "x < y & z"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.xlsx"
            xlsxout.write_workbook(path, plan)
            rows = _rows(path, 1)
        self.assertEqual(rows[1][:3], ["S<1>", "a&b", "disabled"])


class ReadOnlyTransport(unittest.TestCase):
    def test_xml_command_conversion(self):
        self.assertEqual(to_xml_cmd("show jobs all"), "<show><jobs><all/></jobs></show>")
        self.assertEqual(to_xml_cmd("check pending-changes"), "<check><pending-changes/></check>")

    def test_odd_commands_are_refused_before_anything_is_sent(self):
        for bad in ("", "show <x/>", "show system info; request restart"):
            with self.assertRaises(ValueError):
                to_xml_cmd(bad)

    def test_non_read_only_commands_never_leave_the_machine(self):
        api = PanoramaApi("192.0.2.1", "not-a-real-key")
        for command in ("request restart system", "request high-availability state suspend", "debug something", "set cli pager off"):
            with self.assertRaises(PermissionError):
                api.op(command)


if __name__ == "__main__":
    unittest.main()
