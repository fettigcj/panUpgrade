"""`probe-config`: read-only, shows how each configuration view presents the settings, and reports a view that is refused."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.support import sys  # noqa: F401  (puts src on the path)
from panupgrade import cli, probe
from panupgrade.sources import SourceError


def R(text):
    return ET.fromstring(text)


LOCAL = """<response status='success'><result><config><devices><entry name='localhost.localdomain'>
<deviceconfig><system><hostname>fw</hostname></system><setting><tcp><asymmetric-path>bypass</asymmetric-path></tcp></setting></deviceconfig>
<network><profiles><interface-management-profile><entry name='LB-Probe'><ssh>yes</ssh></entry></interface-management-profile></profiles>
<interface><ethernet><entry name='ethernet1/3'><layer3><interface-management-profile>LB-Probe</interface-management-profile></layer3></entry></ethernet></interface></network>
</entry></devices></config></result></response>"""

TEMPLATE = """<response status='success'><result><config><devices><entry name='localhost.localdomain'>
<deviceconfig><setting><session><tcp-reject-non-syn ptpl='tpl-edge'>yes</tcp-reject-non-syn></session></setting></deviceconfig>
</entry></devices></config></result></response>"""


class Extracting(unittest.TestCase):
    def test_it_finds_the_settings_the_profiles_and_the_interface_that_names_one(self):
        found = dict(probe.extract(R(LOCAL)))
        paths = list(found)
        self.assertTrue(any(p.endswith("deviceconfig/setting/tcp") for p in paths), paths)
        self.assertTrue(any(p.endswith("profiles/interface-management-profile") for p in paths), paths)
        self.assertTrue(any("entry[ethernet1/3]" in p and p.endswith("interface-management-profile") for p in paths), paths)
        self.assertIn("<ssh>yes</ssh>", "".join(found.values()))
        self.assertNotIn("hostname", "".join(found.values()))               # nothing else of the configuration is copied out

    def test_attributes_that_say_where_a_node_came_from_are_kept(self):
        (path, xml), = probe.extract(R(TEMPLATE))
        self.assertTrue(path.endswith("deviceconfig/setting/session"))
        self.assertIn("ptpl=\"tpl-edge\"", xml)


class Probing(unittest.TestCase):
    def test_each_view_is_asked_with_a_show_command_only_and_a_refused_view_is_reported(self):
        asked = []

        def op(command):
            asked.append(command)
            if command.endswith("pushed-template"):
                raise SourceError("target is not a Panorama-managed firewall view")
            return R(TEMPLATE if command.endswith("merged") else LOCAL)
        text = probe.probe(op)
        self.assertTrue(all(c.startswith("show config ") for c in asked))
        self.assertEqual(len(asked), len(probe.VIEWS))
        self.assertIn("===== show config pushed-template =====", text)
        self.assertIn("refused or failed: SourceError", text)
        self.assertIn("ptpl=\"tpl-edge\"", text)
        self.assertIn("asymmetric-path", text)

    def test_a_view_without_any_of_the_settings_says_so(self):
        text = probe.probe(lambda c: R("<response status='success'><result><config/></result></response>"), views=("running",))
        self.assertIn("none of the settings appear", text)


class TheCommand(unittest.TestCase):
    def run_it(self, argv):
        args = cli.build_parser().parse_args(argv)
        return cli._probe_config(args)

    def test_it_needs_exactly_one_firewall(self):
        self.assertEqual(self.run_it(["probe-config", "--source", "fake:x.json"]), 2)
        self.assertEqual(self.run_it(["probe-config", "--source", "fake:x.json", "--serial", "A", "--serial", "B"]), 2)

    def test_it_refuses_a_fake_fleet_and_writes_the_report_for_a_live_one(self):
        self.assertEqual(self.run_it(["probe-config", "--source", "fake:x.json", "--serial", "A"]), 2)

        class Live:
            def op_on(self, serial, command):
                return R(LOCAL)
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "_source", lambda args: Live()):
            out = Path(tmp, "probe.txt")
            self.assertEqual(self.run_it(["probe-config", "--source", "panorama:x", "--serial", "S1", "--out", str(out)]), 0)
            self.assertIn("===== show config running =====", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
