"""Certificate-expiry advisories: which builds carry the fix, and what the planner says about a firewall that does not."""

import unittest

from tests.support import device, facts, fleet, fleet_data, ha_facts
from panupgrade import advisories, settings
from panupgrade.planner import Selection, build_plan
from panupgrade.sources import FakeFleet
from panupgrade.versions import Version

DATA = advisories.load()
ROOT_2023, CDSS_2024 = DATA["advisories"]


def plan(version, content, target):
    d = device("FW1", version=version, facts=facts(sw_version=version, content_versions={"app": content, "threat": content, "av": "0", "wildfire": "0"}))
    return build_plan(FakeFleet(fleet_data(d)), target, settings.load(), Selection(accept_standalone_outage=True),
                      {"verified": True, "direct_from": {}, "stepping_install": {}})


def cert(p):
    return next(r for r in p["devices"][0]["results"] if r["check"] == "certificate_advisory")


class Tables(unittest.TestCase):
    def test_a_fixed_hotfix_counts_and_so_do_later_hotfixes_of_the_same_release(self):
        for fixed in ("10.2.1-h1", "10.2.1-h2", "10.2.2-h4", "10.2.8", "10.2.11-h1", "11.1.1", "12.1.10-h2"):
            self.assertTrue(advisories.software_fixed(CDSS_2024, fixed), fixed)

    def test_base_builds_and_early_hotfixes_do_not(self):
        for affected in ("10.2.0", "10.2.1", "10.2.2", "10.2.2-h3", "10.2.7-h2", "10.1.9", "9.1.10", "11.1.0"):
            self.assertFalse(advisories.software_fixed(CDSS_2024, affected), affected)

    def test_the_older_advisory_has_its_own_table(self):
        self.assertFalse(advisories.software_fixed(ROOT_2023, "10.2.3-h8"))
        self.assertTrue(advisories.software_fixed(ROOT_2023, "10.2.3-h9"))
        self.assertTrue(advisories.software_fixed(ROOT_2023, "10.2.4"))

    def test_content_comparison_is_numeric_per_part(self):
        self.assertFalse(advisories.content_fixed(CDSS_2024, "8545-7303"))
        self.assertTrue(advisories.content_fixed(CDSS_2024, "9167-10330"))
        self.assertTrue(advisories.content_fixed(CDSS_2024, "8795-8489"))
        self.assertFalse(advisories.content_fixed(CDSS_2024, "8795-948"))        # 948 < 8489, not a text comparison
        self.assertFalse(advisories.content_fixed(CDSS_2024, "0"))
        self.assertFalse(advisories.content_fixed(CDSS_2024, None))

    def test_assessment_names_the_three_situations(self):
        a = advisories.assess(DATA["advisories"], Version.parse("10.2.1"), "8545-7303", Version.parse("10.2.11-h1"))
        self.assertEqual([s for _, s in a["now"]], ["affected", "affected"])
        b = advisories.assess(DATA["advisories"], Version.parse("10.2.1"), "9167-10330", Version.parse("10.2.11-h1"))
        self.assertEqual([s for _, s in b["now"]], ["fixed", "needs_restart"])      # old advisory: content is enough; new: restart too
        c = advisories.assess(DATA["advisories"], Version.parse("10.2.11-h1"), "8770-8365", Version.parse("10.2.11-h1"))
        self.assertEqual([s for _, s in c["now"]], ["fixed", "fixed"])


class InThePlan(unittest.TestCase):
    def test_an_affected_firewall_gets_advice_and_is_not_blocked(self):
        p = plan("10.2.1", "8545-7303", "10.2.11-h1")
        self.assertEqual(p["devices"][0]["verdict"], "ready")
        self.assertEqual(cert(p)["status"], "warn")
        self.assertEqual(cert(p).get("ack", ""), "")
        self.assertIn(("FW1", "advise_upgrade"), [(a["scope"], a["code"]) for a in p["advice"]])

    def test_the_new_content_without_a_restart_is_called_out_as_needing_one(self):
        self.assertIn("only takes effect after a restart", cert(plan("10.2.1", "9167-10330", "10.2.11-h1"))["reason"])

    def test_a_target_that_is_itself_affected_needs_an_acknowledgement(self):
        p = plan("10.2.1", "9167-10330", "10.2.2")
        self.assertEqual(cert(p)["ack"], "target_cert_advisory")
        self.assertEqual(p["devices"][0]["verdict"], "caution")
        # 10.2.2-h4 has the 2024 fix but not the 2023 root-certificate one (that needs 10.2.3-h9 or 10.2.4 and up)
        self.assertEqual(cert(plan("10.2.1", "9167-10330", "10.2.2-h4"))["ack"], "target_cert_advisory")
        self.assertEqual(cert(plan("10.2.1", "9167-10330", "10.2.8")).get("ack", ""), "")

    def test_a_fixed_build_passes_and_synthetic_future_versions_are_never_flagged(self):
        self.assertEqual(cert(plan("10.2.11", "8770-8365", "10.2.12"))["status"], "pass")
        self.assertEqual(cert(plan("20.2.3", "0", "20.2.9"))["status"], "pass")


class DeviceCertificateAndClockChecks(unittest.TestCase):
    def run_plan(self, cert, clock, pano_clock="2026-10-29T20:22:00+00:00"):
        d = device("FW1", version="10.2.1", facts=facts(sw_version="10.2.1", device_cert=cert, clock_utc=clock))
        data = fleet_data(d)
        data["panorama"]["clock_utc"] = pano_clock
        p = build_plan(FakeFleet(data), "10.2.11-h1", settings.load(), Selection(accept_standalone_outage=True), {"verified": True, "direct_from": {}, "stepping_install": {}})
        return {r["check"]: r for r in p["devices"][0]["results"]}, p

    def test_the_owners_real_case_a_clock_four_hours_behind_makes_the_certificate_not_yet_valid(self):
        results, plan_ = self.run_plan({"status": "success", "validity": "Not yet valid", "not_valid_before": "2026/10/29 13:18:01 PDT"},
                                       "2026-10-29T16:22:00+00:00")
        self.assertEqual(results["device_certificate"]["status"], "warn")
        self.assertIn("clock", results["device_certificate"]["reason"])
        self.assertEqual(results["clock_skew"]["status"], "warn")
        self.assertIn("4.0 h behind", results["clock_skew"]["reason"])
        codes = [(a["scope"], a["code"]) for a in plan_["advice"]]
        self.assertIn(("FW1", "advise_upgrade"), codes)                                  # advice, never a blocker
        self.assertEqual(plan_["devices"][0]["verdict"], "ready")

    def test_a_healthy_firewall_passes_both(self):
        results, _ = self.run_plan({"status": "success", "validity": "Valid", "not_valid_after": "2027/01/27"}, "2026-10-29T20:22:30+00:00")
        self.assertEqual((results["device_certificate"]["status"], results["clock_skew"]["status"]), ("pass", "pass"))

    def test_no_device_certificate_is_called_out_and_a_missing_reading_is_no_data(self):
        results, _ = self.run_plan({"status": "none", "validity": None, "msg": "Device Certificate not found"}, None)
        self.assertEqual(results["device_certificate"]["status"], "warn")
        self.assertEqual((results["clock_skew"]["status"], results["clock_skew"]["kind"]), ("skip", "no_data"))


if __name__ == "__main__":
    unittest.main()
