"""Parsers over canned XML. The XML shapes are UNVERIFIED (written from Upgrade Assurance's parsers, not recorded)."""

import unittest
import xml.etree.ElementTree as ET

from tests.support import TARGET, TEST_RULES
from panupgrade import settings
from panupgrade.model import DeviceRef
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import SourceError
from panupgrade.xmlops import (READ_ONLY_PREFIXES, CannedOp, OpSource, UnscriptedCommand, XmlOpError, collect_facts, ok,
                               parse_free_mb, parse_ha)

DISK = ("Filesystem Size Used Avail Use% Mounted on\n/dev/root 7.0G 3.9G 2.7G 59% /\n"
        "/dev/sda5 9.0G 0.5G 8.4G 6% /opt/panrepo\n/dev/sda6 20G 1G 19G 5% /opt/panlogs\n")


def ha(state="active", peer="passive", sync="synchronized", mode="Active-Passive", state_sync="Complete", links="up"):
    """Shape recorded from PAN-OS 10.2.1 / 10.2.11-h1 on 2026-10-29 (trimmed): state-sync lives under local-info."""
    sync_el = f"<state-sync>{state_sync}</state-sync>" if state_sync else ""
    conns = "".join(f"<{c}><conn-status>{links}</conn-status></{c}>" for c in ("conn-ha1", "conn-ha2")) if links else ""
    return ok(f"<enabled>yes</enabled><group><mode>{mode}</mode><local-info><state>{state}</state>"
              f"<preemptive>no</preemptive>{sync_el}</local-info><peer-info><state>{peer}</state>{conns}</peer-info>"
              f"<running-sync>{sync}</running-sync></group>")


def responses(**over):
    base = {
        "show system info": ok("<system><sw-version>20.2.3</sw-version><app-version>8545-7303</app-version><threat-version>0</threat-version>"
                               "<av-version>0</av-version><wildfire-version>0</wildfire-version></system>"),
        "check pending-changes": ok("no"),
        "show jobs all": ok("<job><status>FIN</status></job><job><status>FIN</status></job>"),
        "show session info": ok("<num-active>1000</num-active>"),
        "show system disk-space": ok(DISK),
        "show high-availability state": ha(),
        "show device-certificate status": ok("<device-certificate><msg>Successfully fetched Device Certificate</msg><status>success</status>"
                                              "<validity>Valid</validity><not_valid_before>2026/10/29 13:18:01 PDT</not_valid_before>"
                                              "<not_valid_after>2027/01/27 12:18:00 PST</not_valid_after></device-certificate>"),
        "show clock": ok("Thu Oct 29 13:22:23 PDT 2026"),
        "request system software info": ok("<sw-updates><versions><entry><version>20.2.3</version><downloaded>yes</downloaded></entry>"
                                           "<entry><version>20.2.9</version><downloaded>no</downloaded></entry></versions></sw-updates>"),
    }
    base.update(over)
    return base


class Parsers(unittest.TestCase):
    def test_full_collection(self):
        op = CannedOp(responses())
        facts, problems = collect_facts(op, "PEER1")
        self.assertEqual(problems, [])
        self.assertEqual((facts.sw_version, facts.pending_changes, facts.running_jobs, facts.session_count, facts.disk_free_mb),
                         ("20.2.3", False, 0, 1000, int(8.4 * 1024)))
        self.assertEqual((facts.ha_mode, facts.ha_state, facts.ha_config_sync, facts.ha_session_sync, facts.peer_serial),
                         ("active-passive", "active", True, True, "PEER1"))

    def test_only_read_only_commands_are_issued(self):
        op = CannedOp(responses())
        collect_facts(op)
        self.assertTrue(op.calls)
        for call in op.calls:
            self.assertTrue(call.startswith(READ_ONLY_PREFIXES), call)

    def test_unscripted_command_fails_loudly(self):
        with self.assertRaises(UnscriptedCommand):
            CannedOp({})("show something else")

    def test_a_failed_query_leaves_that_fact_none_and_reports_why(self):
        bad = responses(**{"show jobs all": "<response status='error'><result/></response>"})
        facts, problems = collect_facts(CannedOp(bad))
        self.assertIsNone(facts.running_jobs)
        self.assertEqual(facts.sw_version, "20.2.3")
        self.assertTrue(any("jobs" in p for p in problems))

    def test_missing_pieces_are_none_not_guessed(self):
        sparse = responses(**{"show high-availability state": ha(state_sync="", links="")})   # no state-sync or link elements
        facts, _ = collect_facts(CannedOp(sparse))
        self.assertIsNone(facts.ha_session_sync)
        self.assertTrue(facts.ha_config_sync)

    def test_standalone_and_running_jobs(self):
        op = CannedOp(responses(**{"show high-availability state": ok("<enabled>no</enabled>"),
                                   "show jobs all": ok("<job><status>ACT</status></job><job><status>FIN</status></job>")}))
        facts, _ = collect_facts(op)
        self.assertEqual((facts.ha_mode, facts.running_jobs), ("none", 1))

    def test_disk_units(self):
        mk = lambda avail: CannedOp({"show system disk-space": ok(f"hdr\n/dev/x 1G 1G {avail} 1% /opt/panrepo\n")})
        self.assertEqual(parse_free_mb(mk("512M")), 512)
        self.assertEqual(parse_free_mb(mk("2G")), 2048)
        self.assertIsNone(parse_free_mb(mk("lots")))
        self.assertIsNone(parse_free_mb(CannedOp({"show system disk-space": ok("hdr\n")})))

    def test_active_active_mode_is_reported_as_such(self):
        self.assertEqual(parse_ha(CannedOp({"show high-availability state": ha(mode="Active-Active")}))["ha_mode"], "active-active")

    def test_bad_xml_status_raises(self):
        with self.assertRaises(XmlOpError):
            parse_free_mb(CannedOp({"show system disk-space": "<response status='error'/>"}))


class OpSourceFeedsThePlanner(unittest.TestCase):
    def test_plan_from_canned_devices(self):
        refs = [DeviceRef("OP0001", "op-fw1", "SIM-A", "20.2.3", True), DeviceRef("OP0002", "op-fw2", "SIM-A", "20.2.3", True)]
        ops = {"OP0001": CannedOp(responses(**{"show high-availability state": ok("<enabled>no</enabled>")})),
               "OP0002": CannedOp({c: "<response status='error'/>" for c in responses()})}
        src = OpSource("22.1.0", refs, lambda serial: ops[serial])
        plan = build_plan(src, TARGET, settings.load(), Selection(accept_standalone_outage=True), rules=TEST_RULES)
        verdicts = {d["serial"]: d["verdict"] for d in plan["devices"]}
        self.assertEqual(verdicts["OP0001"], "ready")
        self.assertEqual(verdicts["OP0002"], "skipped")         # nothing could be read: no data, not a pass
        with self.assertRaises(SourceError):
            src.facts("NOPE")


if __name__ == "__main__":
    unittest.main()


class HaLinkAndSyncShapes(unittest.TestCase):
    def facts_for(self, **kw):
        op = CannedOp(responses(**{"show high-availability state": ha(**kw)}))
        return collect_facts(op, "PEER")[0]

    def test_session_sync_and_links_are_read_from_the_recorded_shape(self):
        f = self.facts_for()
        self.assertEqual((f.ha_session_sync, f.ha_links_ok, f.ha_config_sync), (True, True, True))

    def test_each_reading_can_be_bad_independently(self):
        self.assertEqual(self.facts_for(state_sync="Incomplete").ha_session_sync, False)
        self.assertEqual(self.facts_for(links="down").ha_links_ok, False)
        self.assertEqual(self.facts_for(sync="not synchronized").ha_config_sync, False)


class ContentVersions(unittest.TestCase):
    def test_reads_the_content_versions_the_firewall_reports(self):
        f = collect_facts(CannedOp(responses()), None)[0]
        self.assertEqual(f.content_versions["app"], "8545-7303")
        self.assertEqual(f.content_versions["av"], "0")
        self.assertIsNone(f.content_versions["url"])           # not reported in the canned reply: no data, not a guess


class DeviceCertificateAndClock(unittest.TestCase):
    def test_reads_the_certificate_status_as_the_firewall_words_it(self):
        f = collect_facts(CannedOp(responses()), None)[0]
        self.assertEqual((f.device_cert["status"], f.device_cert["validity"]), ("success", "Valid"))

    def test_not_yet_valid_is_reported_as_such(self):
        bad = ok("<device-certificate><status>success</status><validity>Not yet valid</validity></device-certificate>")
        f = collect_facts(CannedOp(responses(**{"show device-certificate status": bad})), None)[0]
        self.assertEqual(f.device_cert["validity"], "Not yet valid")

    def test_a_firewall_with_no_certificate_says_so_instead_of_guessing(self):
        none = ok("Device Certificate not found")
        f = collect_facts(CannedOp(responses(**{"show device-certificate status": none})), None)[0]
        self.assertEqual((f.device_cert["status"], f.device_cert["validity"]), ("none", None))

    def test_the_clock_becomes_utc_and_an_unknown_zone_is_never_guessed(self):
        from panupgrade.xmlops import parse_clock_text
        self.assertEqual(parse_clock_text("Thu Oct 29 13:22:23 PDT 2026"), "2026-10-29T20:22:23+00:00")
        self.assertEqual(parse_clock_text("Thu Jan  1 00:00:00 UTC 2026"), "2026-01-01T00:00:00+00:00")
        self.assertIsNone(parse_clock_text("Thu Oct 29 13:22:23 ZZZ 2026"))
        self.assertIsNone(parse_clock_text(None))
