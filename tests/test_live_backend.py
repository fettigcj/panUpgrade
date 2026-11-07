"""The live backend's logic against a scripted API: no network, no waiting."""

import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.support import sys  # noqa: F401  (puts src on the path)
from panupgrade import settings
from panupgrade.executor import BackendError
from panupgrade.sources import SourceError
from panupgrade_live.backend import LiveBackend, uptime_seconds
from panupgrade_live.panorama import WriteApi, PanoramaApi


def R(text):
    return ET.fromstring(text)


def ok(inner=""):
    return R(f"<response status='success'><result>{inner}</result></response>")


class Scripted:
    """Replies per (kind, command, target), consumed in order; the last one repeats."""

    def __init__(self, script):
        self.script, self.log = {k: list(v) for k, v in script.items()}, []

    def _next(self, key):
        self.log.append(key)
        if key not in self.script:
            raise AssertionError(f"unscripted: {key}")
        items = self.script[key]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    def op(self, command, target=None):
        return self._next(("op", command, target))

    def op_xml(self, xml, target=None):
        return self._next(("xml", xml, target))

    def write_xml(self, xml, target):
        return self._next(("write", xml, target))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def backend(read, write=None, tmp=None, **opts):
    clock = Clock()
    s = settings.load(None, [f"{k}={v}" for k, v in opts.items()])
    b = LiveBackend(read, write or read, s, tmp or tempfile.gettempdir(), sleep=clock.sleep, clock=clock)
    return b


JOB = lambda status, result="OK", lines="": ok(f"<job><status>{status}</status><result>{result}</result><details>{lines}</details></job>")
INFO_ENTRIES = lambda *e: ok("<sw-updates>" + "".join(f"<entry><version>{v}</version><downloaded>{d}</downloaded></entry>" for v, d in e) + "</sw-updates>")
CHECK = ("xml", "<request><system><software><check/></software></system></request>", "S1")
INFO = ("xml", "<request><system><software><info/></software></system></request>", "S1")
DL = ("write", "<request><system><software><download><version>10.2.2</version></download></software></system></request>", "S1")
INS = ("write", "<request><system><software><install><version>10.2.2</version></install></software></system></request>", "S1")
JOBQ = lambda n: ("xml", f"<show><jobs><id>{n}</id></jobs></show>", "S1")


class Downloads(unittest.TestCase):
    def test_an_image_already_on_the_box_is_not_downloaded_again(self):
        api = Scripted({CHECK: [ok()], INFO: [INFO_ENTRIES(("10.2.2", "yes"))]})
        backend(api).download("S1", "10.2.2")
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_a_version_the_update_server_does_not_offer_is_an_error_before_anything_is_sent(self):
        api = Scripted({CHECK: [ok()], INFO: [INFO_ENTRIES(("10.2.1", "yes"))]})
        with self.assertRaisesRegex(BackendError, "not offered"):
            backend(api).download("S1", "10.2.2")
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_download_waits_for_the_job_to_finish(self):
        api = Scripted({CHECK: [ok()], INFO: [INFO_ENTRIES(("10.2.2", "no"))], DL: [ok("<job>12</job>")],
                        JOBQ(12): [JOB("ACT"), JOB("ACT"), JOB("FIN")]})
        backend(api).download("S1", "10.2.2")
        self.assertEqual([k for k in api.log if k == JOBQ(12)].__len__(), 3)

    def test_a_failed_job_is_an_error_with_the_devices_own_words(self):
        api = Scripted({CHECK: [ok()], INFO: [INFO_ENTRIES(("10.2.2", "no"))], DL: [ok("<job>12</job>")],
                        JOBQ(12): [JOB("FIN", "FAIL", "<line>Failed to download: no space</line>")]})
        with self.assertRaisesRegex(BackendError, "no space"):
            backend(api).download("S1", "10.2.2")

    def test_a_job_that_never_finishes_times_out(self):
        api = Scripted({CHECK: [ok()], INFO: [INFO_ENTRIES(("10.2.2", "no"))], DL: [ok("<job>12</job>")], JOBQ(12): [JOB("ACT")]})
        with self.assertRaisesRegex(BackendError, "did not finish within 100 s"):
            backend(api, download_timeout_s=100).download("S1", "10.2.2")

    def test_the_job_id_can_come_from_the_message_text(self):
        api = Scripted({INS: [R("<response status='success'><msg><line>Install job enqueued with jobid 7</line></msg></response>")],
                        JOBQ(7): [JOB("FIN")]})
        backend(api).install("S1", "10.2.2")

    def test_a_refused_command_is_an_error(self):
        api = Scripted({INS: [R("<response status='error'><msg><line>Software manager is busy</line></msg></response>")]})
        with self.assertRaisesRegex(BackendError, "busy"):
            backend(api).install("S1", "10.2.2")


class DeleteImage(unittest.TestCase):
    SYSINFO = ("op", "show system info", "S1")
    DELETE = ("write", "<delete><software><version>10.2.1</version></software></delete>", "S1")

    @staticmethod
    def images(*rows):
        return ok("<sw-updates><versions>" + "".join(
            f"<entry><version>{v}</version><downloaded>{d}</downloaded><current>{c}</current></entry>" for v, d, c in rows) + "</versions></sw-updates>")

    def sysinfo(self, version="10.2.4"):
        return ok(f"<system><sw-version>{version}</sw-version></system>")

    def test_it_sends_the_delete_and_checks_the_image_is_gone(self):
        api = Scripted({self.SYSINFO: [self.sysinfo()],
                        INFO: [self.images(("10.2.1", "yes", "no"), ("10.2.4", "yes", "yes")), self.images(("10.2.1", "no", "no"), ("10.2.4", "yes", "yes"))],
                        self.DELETE: [ok("deleted")]})
        self.assertEqual(backend(api).delete_image("S1", "10.2.1"), {"deleted": "10.2.1"})
        self.assertIn(self.DELETE, api.log)

    def test_the_running_image_is_refused_before_anything_is_sent(self):
        api = Scripted({self.SYSINFO: [self.sysinfo()], INFO: [self.images(("10.2.4", "yes", "yes"))]})
        with self.assertRaisesRegex(BackendError, "is the image that is running"):
            backend(api).delete_image("S1", "10.2.4")
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_an_image_that_is_not_there_is_not_an_error_and_sends_nothing(self):
        api = Scripted({self.SYSINFO: [self.sysinfo()], INFO: [self.images(("10.2.1", "no", "no"))]})
        self.assertEqual(backend(api).delete_image("S1", "10.2.1"), {"already_absent": "10.2.1"})
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_an_image_that_stays_after_the_delete_is_an_error(self):
        api = Scripted({self.SYSINFO: [self.sysinfo()], INFO: [self.images(("10.2.1", "yes", "no"))], self.DELETE: [ok("deleted")]})
        with self.assertRaisesRegex(BackendError, "to be gone"):
            backend(api).delete_image("S1", "10.2.1")


class Reboot(unittest.TestCase):
    INFOQ = ("op", "show system info", "S1")
    RESTART = ("write", "<request><restart><system/></restart></request>", "S1")

    def sysinfo(self, uptime, version="10.2.1"):
        return ok(f"<system><uptime>{uptime}</uptime><sw-version>{version}</sw-version></system>")

    def test_it_waits_through_the_old_boot_and_the_outage_for_the_new_boot(self):
        api = Scripted({self.INFOQ: [self.sysinfo("0 days, 5:00:00"), self.sysinfo("0 days, 5:00:30"), SourceError("not connected"),
                                     SourceError("not connected"), self.sysinfo("0 days, 0:01:10", "10.2.2")],
                        self.RESTART: [ok("restarting")]})
        b = backend(api)
        b.state = lambda serial: {"running_jobs": 0}
        self.assertEqual(b.reboot_and_wait("S1", 600), "10.2.2")

    def test_a_box_that_never_reboots_is_not_mistaken_for_one_that_did(self):
        api = Scripted({self.INFOQ: [self.sysinfo("0 days, 5:00:00"), self.sysinfo("0 days, 5:09:00")], self.RESTART: [ok()]})
        b = backend(api)
        b.state = lambda serial: {"running_jobs": 0}
        with self.assertRaisesRegex(BackendError, "did not happen within 300 s"):
            b.reboot_and_wait("S1", 300)

    def test_unreadable_uptime_refuses_before_sending_the_restart(self):
        api = Scripted({self.INFOQ: [ok("<system><uptime>soon</uptime></system>")]})
        with self.assertRaisesRegex(BackendError, "uptime"):
            backend(api).reboot_and_wait("S1", 300)
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_uptime_parsing(self):
        self.assertEqual(uptime_seconds("1 days, 2:03:04"), 93784)
        self.assertEqual(uptime_seconds("0 days, 0:00:09"), 9)
        self.assertIsNone(uptime_seconds("weird"))


class BackupAndSync(unittest.TestCase):
    CFG = ("xml", "<show><config><running/></config></show>", "S1")
    INFOQ = ("op", "show system info", "S1")

    def test_a_good_backup_is_written_parsed_back_and_hashed(self):
        cfg = ok("<config version='10.2.0'><devices><entry name='localhost.localdomain'/></devices></config>")
        api = Scripted({self.CFG: [cfg], self.INFOQ: [ok("<system><hostname>fw-a</hostname></system>")]})
        with tempfile.TemporaryDirectory() as tmp:
            result = backend(api, tmp=tmp).backup("S1")
            data = Path(result["path"]).read_bytes()
        self.assertTrue(result["parsed_ok"])
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertIn("fw-a_S1_", result["path"])

    def test_a_reply_without_a_configuration_is_not_a_backup(self):
        api = Scripted({self.CFG: [ok("<something-else/>")]})
        self.assertFalse(backend(api).backup("S1")["parsed_ok"])

    def test_sync_waits_for_the_config_sync_to_read_synchronized(self):
        api = Scripted({("write", "<request><high-availability><sync-to-remote><running-config/></sync-to-remote></high-availability></request>", "S1"): [ok("sent")]})
        b = backend(api)
        readings = iter([False, False, True])
        b.state = lambda serial: {"ha_config_sync": next(readings)}
        b.sync_to_peer("S1")
        self.assertEqual(list(readings), [])      # all three readings were consumed


class ContentUpdates(unittest.TestCase):
    CHECK = ("op", "request content upgrade check", "S1")
    SYSINFO = ("op", "show system info", "S1")
    DL = ("write", "<request><content><upgrade><download><latest/></download></upgrade></content></request>", "S1")
    INS = ("write", "<request><content><upgrade><install><version>latest</version></install></upgrade></content></request>", "S1")

    def check(self, *versions):
        return ok("<content-updates>" + "".join(
            f"<entry><version>{v}</version><latest>{'yes' if i == 0 else 'no'}</latest></entry>" for i, v in enumerate(versions)) + "</content-updates>")

    def sysinfo(self, app):
        return ok(f"<system><app-version>{app}</app-version><threat-version>0</threat-version></system>")

    def test_installs_the_latest_when_the_installed_version_differs(self):
        api = Scripted({self.CHECK: [self.check("8770-8365", "8545-7303")], self.SYSINFO: [self.sysinfo("8545-7303")],
                        self.DL: [ok("<job>3</job>")], self.INS: [ok("<job>4</job>")],
                        JOBQ(3): [JOB("FIN")], JOBQ(4): [JOB("ACT"), JOB("FIN")]})
        self.assertEqual(backend(api).update_content("S1", "content"), {"installed": "8770-8365"})
        self.assertEqual([k[0] for k in api.log if k[0] == "write"], ["write", "write"])

    def test_nothing_is_sent_when_the_latest_is_already_installed(self):
        api = Scripted({self.CHECK: [self.check("8770-8365")], self.SYSINFO: [self.sysinfo("8770-8365")]})
        self.assertEqual(backend(api).update_content("S1", "content"), {"already_current": "8770-8365"})
        self.assertNotIn("write", [k[0] for k in api.log])

    def test_no_update_offered_is_an_error_with_the_raw_reply(self):
        api = Scripted({self.CHECK: [ok("<content-updates/>")]})
        with self.assertRaisesRegex(BackendError, "no content update is offered"):
            backend(api).update_content("S1", "content")

    def test_the_latest_is_the_highest_version_when_the_list_carries_no_latest_flag(self):
        # the shape seen on real PAN-OS 10.2 firewalls: entries in no particular order, no <latest> element at all
        unflagged = ok("<content-updates>" + "".join(f"<entry><version>{v}</version><downloaded>no</downloaded><current>no</current></entry>"
                                                    for v in ("9100-9999", "9154-10270", "9154-9870", "8545-7303")) + "</content-updates>")
        api = Scripted({self.CHECK: [unflagged], self.SYSINFO: [self.sysinfo("8545-7303")], self.DL: [ok("<job>3</job>")],
                        self.INS: [ok("<job>4</job>")], JOBQ(3): [JOB("FIN")], JOBQ(4): [JOB("FIN")]})
        self.assertEqual(backend(api).update_content("S1", "content"), {"installed": "9154-10270"})

    def test_reports_the_installed_versions_by_key(self):
        api = Scripted({self.SYSINFO: [ok("<system><app-version>1-1</app-version><threat-version>0</threat-version>"
                                          "<av-version>5</av-version><wildfire-version>0</wildfire-version></system>")]})
        got = backend(api).content_versions("S1")
        self.assertEqual((got["app"], got["threat"], got["av"], got["wildfire"], got["url"]), ("1-1", "0", "5", "0", None))


class OnlyAllowListedCommandsCanBeSent(unittest.TestCase):
    def api(self):
        return WriteApi("192.0.2.1", key="not-a-key")        # any attempt to send would need the network; refusals come first

    def test_refused_before_anything_leaves_the_machine(self):
        w = self.api()
        for bad in (
            "<request><restart><system/></restart></request><request><restart><system/></restart></request>",
            "<request><content><upgrade><download><latest/></download></upgrade></anti-virus></request>",       # mismatched tags
            "<request><content><upgrade><install><version>8770-8365</version></install></upgrade></content></request>",   # only "latest"
            "<request><url-filtering><upgrade><download><latest/></download></upgrade></url-filtering></request>",
            "<request><system><software><install><version>10.2.2; reboot</version></install></software></system></request>",
            "<request><system><software><install><version>10.2</version></install></software></system></request>",
            "<request><shutdown><system/></shutdown></request>",
            "<request><high-availability><state><suspend/></state></high-availability></request><show><jobs/></show>",
            "<delete><config/></delete>",
            "<commit/>",
        ):
            with self.assertRaises(PermissionError, msg=bad):
                w.write_xml(bad, "S1")

    def test_a_write_must_name_a_firewall_never_panorama_itself(self):
        with self.assertRaises(PermissionError):
            self.api().write_xml("<request><restart><system/></restart></request>", None)

    def test_the_read_connection_cannot_write_and_cannot_chain_a_second_command(self):
        r = PanoramaApi("192.0.2.1", key="not-a-key")
        self.assertFalse(hasattr(r, "write_xml"))
        for bad in ("<request><restart><system/></restart></request>",
                    "<show><jobs/></show><request><restart><system/></restart></request>"):
            with self.assertRaises(PermissionError, msg=bad):
                r.op_xml(bad, "S1")


class TheAsymmetryWindowOnARealFirewall(unittest.TestCase):
    """Reply shapes are UNVERIFIED against a firewall; these pin what the code does with the shapes it expects, and that it refuses to guess."""
    SESSION = "<show><session><info/></session></show>"
    CONFIG = "<show><config><running/></config></show>"

    def read_script(self, flag="True", path=None, config=None):
        node = f"<asymmetric-path>{path}</asymmetric-path>" if path else ""
        session = f"<session><tcp-reject-non-syn>{config}</tcp-reject-non-syn></session>" if config else ""
        return {("xml", self.SESSION, "S1"): [ok(f"<tcp-reject-non-syn>{flag}</tcp-reject-non-syn>")],
                ("xml", self.CONFIG, "S1"): [R(f"<response status='success'><result><config><devices><entry><deviceconfig><setting><tcp>{node}</tcp>{session}</setting></deviceconfig></entry></devices></config></result></response>")]}

    def test_it_reads_both_values_and_none_means_not_configured(self):
        self.assertEqual(backend(Scripted(self.read_script("True"))).tcp_asymmetry("S1"), {"reject_non_syn": True, "reject_non_syn_config": None, "asymmetric_path": None})
        self.assertEqual(backend(Scripted(self.read_script("False", "bypass", "no"))).tcp_asymmetry("S1"),
                         {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "bypass"})

    def test_it_refuses_to_guess_what_it_cannot_read(self):
        script = self.read_script()
        script[("xml", self.SESSION, "S1")] = [ok("<something-else>x</something-else>")]
        with self.assertRaises(BackendError):
            backend(Scripted(script)).tcp_asymmetry("S1")
        with self.assertRaises(BackendError):
            backend(Scripted(self.read_script("True", "global"))).tcp_asymmetry("S1")

    def test_setting_edits_and_commits_on_the_firewall_and_only_then_changes_the_runtime_flag(self):
        order = []

        class Write(Scripted):
            def write_config(self, knob, action, value, target):
                order.append(("config", knob, action, value))
                return ok()

            def commit_local(self, target):
                order.append(("commit",))
                return ok("<job>77</job>")

            def write_xml(self, xml, target):
                order.append(("op", xml))
                return R("<response status='success'/>")
        w = Write({("xml", "<show><jobs><id>77</id></jobs></show>", "S1"): [JOB("FIN")]})
        backend(w, w).set_tcp_asymmetry("S1", False, "no", "bypass")
        self.assertEqual(order, [("config", "tcp-reject-non-syn", "set", "no"), ("config", "asymmetric-path", "set", "bypass"), ("commit",), ("op", "<set><session><tcp-reject-non-syn>no</tcp-reject-non-syn></session></set>")])
        order.clear()
        backend(w, w).set_tcp_asymmetry("S1", True, None, None)
        self.assertEqual(order[:2], [("config", "tcp-reject-non-syn", "delete", None), ("config", "asymmetric-path", "delete", None)])

    def test_only_the_asymmetric_path_may_be_edited_and_only_those_ways(self):
        from panupgrade_live.panorama import WRITE_ALLOWED, _config_params
        w = WriteApi("192.0.2.1", key="not-a-key")
        for knob, action, value in (("asymmetric-path", "set", "allow"), ("asymmetric-path", "set", None), ("asymmetric-path", "delete", "bypass"),
                                    ("asymmetric-path", "edit", "bypass"), ("asymmetric-path", "set", "bypass; drop"), ("tcp-reject-non-syn", "set", "bypass"),
                                    ("tcp-reject-non-syn", "set", "maybe"), ("hostname", "set", "x")):
            with self.assertRaises(PermissionError, msg=(knob, action, value)):
                w.write_config(knob, action, value, "S1")
        with self.assertRaises(PermissionError):
            w.write_config("asymmetric-path", "set", "bypass", None)
        with self.assertRaises(PermissionError):
            w.commit_local(None)
        self.assertIn("asymmetric-path", _config_params("asymmetric-path", "set", "bypass")["element"])
        self.assertTrue(_config_params("asymmetric-path", "delete", None)["xpath"].endswith("/tcp/asymmetric-path"))
        self.assertEqual(_config_params("tcp-reject-non-syn", "set", "no")["element"], "<tcp-reject-non-syn>no</tcp-reject-non-syn>")
        self.assertTrue(_config_params("tcp-reject-non-syn", "delete", None)["xpath"].endswith("/setting/session/tcp-reject-non-syn"))
        allowed = lambda xml: any(p.fullmatch(xml) for p in WRITE_ALLOWED)
        self.assertTrue(allowed("<set><session><tcp-reject-non-syn>no</tcp-reject-non-syn></session></set>"))
        for bad in ("<set><session><tcp-reject-non-syn>maybe</tcp-reject-non-syn></session></set>", "<set><session><other>no</other></session></set>"):
            self.assertFalse(allowed(bad), bad)


class TheLoadBalancerDrainOnARealFirewall(unittest.TestCase):
    CONFIG = "<show><config><running/></config></show>"

    def config(self, body):
        return R(f"<response status='success'><result><config><devices><entry><network><profiles><interface-management-profile>{body}"
                 f"</interface-management-profile></profiles></network></entry></devices></config></result></response>")

    def test_it_reads_the_named_profile_and_not_the_others(self):
        w = Scripted({("xml", self.CONFIG, "S1"): [self.config("<entry name='Other'><ssh>yes</ssh></entry><entry name='LB'><ssh>yes</ssh><http>no</http></entry>")]})
        self.assertEqual(backend(w).lb_profile("S1", "LB", ["ssh", "http", "https"]), {"exists": True, "values": {"ssh": "yes", "http": "no", "https": None}})
        w = Scripted({("xml", self.CONFIG, "S1"): [self.config("<entry name='Other'/>")]})
        self.assertEqual(backend(w).lb_profile("S1", "LB", ["ssh"]), {"exists": False, "values": {"ssh": None}})

    def test_it_refuses_a_value_it_cannot_put_back(self):
        w = Scripted({("xml", self.CONFIG, "S1"): [self.config("<entry name='LB'><ssh>maybe</ssh></entry>")]})
        with self.assertRaises(BackendError):
            backend(w).lb_profile("S1", "LB", ["ssh"])

    def test_the_edits_come_before_one_commit(self):
        order = []

        class Write(Scripted):
            def write_profile(self, profile, protocol, action, value, target):
                order.append((profile, protocol, action, value))
                return ok()

            def commit_local(self, target):
                order.append(("commit",))
                return ok("<job>9</job>")
        w = Write({("xml", "<show><jobs><id>9</id></jobs></show>", "S1"): [JOB("FIN")]})
        backend(w, w).set_lb_profile("S1", "LB", {"ssh": "no", "http": None})
        self.assertEqual(order, [("LB", "ssh", "set", "no"), ("LB", "http", "delete", None), ("commit",)])

    def test_only_one_protocol_of_a_named_profile_can_be_edited(self):
        from panupgrade_live.panorama import _profile_params
        w = WriteApi("192.0.2.1", key="not-a-key")
        for args in (("LB", "telnet", "set", "no"), ("LB", "ssh", "set", "maybe"), ("LB", "ssh", "delete", "no"), ("LB]/x[", "ssh", "set", "no"),
                     ("", "ssh", "set", "no"), ("LB", "ssh", "edit", "no")):
            with self.assertRaises(PermissionError, msg=args):
                w.write_profile(*args, "S1")
        with self.assertRaises(PermissionError):
            w.write_profile("LB", "ssh", "set", "no", None)
        got = _profile_params("LB-Probe", "ssh", "set", "no")
        self.assertIn("interface-management-profile/entry[@name='LB-Probe']", got["xpath"])
        self.assertEqual(got["element"], "<ssh>no</ssh>")
        self.assertTrue(_profile_params("LB-Probe", "ssh", "delete", None)["xpath"].endswith("entry[@name='LB-Probe']/ssh"))


if __name__ == "__main__":
    unittest.main()
