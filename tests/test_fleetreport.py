# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""`report` says what is wrong with the fleet before any upgrade is planned."""

import unittest

from panupgrade import console, fleetreport

STYLE = console.Style(False, False)


def dev(serial, name, version="10.2.1", peer="", **facts):
    return {"serial": serial, "hostname": name, "model": "M", "sw_version": version, "connected": True, "peer_serial": peer,
            "ha_state": facts.get("ha_state", ""), "facts": facts}


class MixedVersionPairs(unittest.TestCase):
    def test_a_pair_on_different_versions_is_not_called_unsynchronised_because_it_cannot_be(self):
        r = fleetreport.build({"panorama": {"version": "12.2.0"}, "devices": [
            dev("A", "a1", "10.2.2", "B", ha_config_sync=False), dev("B", "a2", "10.2.3", "A", ha_config_sync=False)]})
        text = " | ".join(p for _n, ps in r["attention"] for p in ps)
        self.assertIn("different versions", text)
        self.assertNotIn("not synchronized", text)

    def test_the_same_pair_on_one_version_still_is(self):
        r = fleetreport.build({"panorama": {"version": "12.2.0"}, "devices": [
            dev("A", "a1", "10.2.2", "B", ha_config_sync=False), dev("B", "a2", "10.2.2", "A", ha_config_sync=False)]})
        self.assertIn("not synchronized", " | ".join(p for _n, ps in r["attention"] for p in ps))


class Lagging(unittest.TestCase):
    """The report says which firewalls lag behind Panorama, once, without waiting for a plan."""

    def build(self, *devices, panorama="12.2.0", **over):
        from panupgrade import settings
        return fleetreport.build({"panorama": {"version": panorama}, "devices": list(devices)}, settings.load(None, [f"{k}={v}" for k, v in over.items()]))

    def test_every_firewall_more_than_the_limit_behind_is_named_in_one_note(self):
        r = self.build(dev("A", "a1", "10.2.1"), dev("B", "a2", "10.2.2"), dev("C", "c1", "12.1.4"))
        [note] = r["advice"]
        self.assertEqual(note["title"], "PAN-OS version lagging behind Panorama")
        self.assertEqual([(d["serial"], d["hostname"], d["version"]) for d in note["devices"]], [("A", "a1", "10.2.1"), ("B", "a2", "10.2.2")])
        out = fleetreport.render(r, STYLE)
        self.assertEqual(out.count("PAN-OS version lagging behind Panorama"), 1)
        self.assertEqual(out.count("Panorama converts"), 1)
        self.assertRegex(out, r"\n      A  a1  10\.2\.1\n      B  a2  10\.2\.2")
        self.assertNotIn("c1  12.1.4", out.split("PAN-OS versions")[0])           # one release behind: within the limit

    def test_the_limit_and_the_unit_are_the_settings(self):
        self.assertEqual(self.build(dev("A", "a1", "10.2.1"), spread_limit=9)["advice"], [])
        self.assertEqual(len(self.build(dev("A", "a1", "11.2.3"), panorama="12.2.0", panorama_compare="minor", spread_limit=1)["advice"]), 1)

    def test_nothing_is_said_when_nobody_lags_or_there_are_no_settings_to_judge_by(self):
        self.assertEqual(self.build(dev("A", "a1", "12.2.1"))["advice"], [])
        self.assertEqual(fleetreport.build({"panorama": {"version": "12.2"}, "devices": [dev("A", "a1", "10.2.1")]})["advice"], [])

    def test_a_version_that_cannot_be_read_is_skipped_not_fatal(self):
        self.assertEqual(self.build(dev("A", "a1", "unknown"), dev("B", "b1", ""))["advice"], [])

    def test_the_plan_words_it_the_same_way(self):
        from panupgrade import checks
        [note] = self.build(dev("A", "a1", "10.2.1"))["advice"]
        self.assertTrue(note["text"].startswith(checks.lagging_text(1, 2, "minor", "12.2.0")))


class FleetReport(unittest.TestCase):
    def report(self, *devices):
        return fleetreport.build({"panorama": {"version": "12.1"}, "devices": list(devices)})

    def problems(self, report):
        return {name: " | ".join(p) for name, p in report["attention"]}

    def test_a_healthy_pair_needs_no_attention(self):
        r = self.report(dev("A", "a1", peer="B", ha_config_sync=True), dev("B", "a2", peer="A", ha_config_sync=True))
        self.assertEqual(r["attention"], [])
        self.assertEqual((r["pairs"], r["standalone"]), (1, 0))

    def test_pair_on_different_versions_and_content_is_called_out(self):
        r = self.report(dev("A", "a1", "10.2.2", "B", content_versions={"app": "9167-10330"}),
                        dev("B", "a2", "10.2.1", "A", content_versions={"app": "8545-7303"}))
        text = self.problems(r)["a1"]
        self.assertIn("different versions: a1 10.2.2, a2 10.2.1", text)
        self.assertIn("different app content", text)

    def test_unsynced_unknown_compat_pending_and_offline(self):
        r = self.report(dev("A", "a1", peer="B", ha_config_sync=False, pending_changes=True,
                            ha_content_compat={"app": "Unknown", "threat": "Mismatch"}),
                        dev("B", "a2", peer="A"),
                        {**dev("C", "c1"), "connected": False})
        text = self.problems(r)
        self.assertIn("not synchronized", text["a1"])
        self.assertIn("does not match its HA peer (threat)", text["a1"])
        self.assertIn("cannot tell whether content matches", text["a1"])
        self.assertIn("uncommitted", text["a1"])
        self.assertIn("not connected", text["c1"])

    def test_the_url_database_never_counts_against_a_pair(self):
        compat = {"build": "Match", "app": "Match", "threat": "Match", "av": "Match", "url": "Mismatch", "iot": "Unknown"}
        r = self.report(dev("A", "a1", peer="B", ha_config_sync=True, ha_content_compat=compat),
                        dev("B", "a2", peer="A", ha_config_sync=True, ha_content_compat=compat))
        self.assertEqual(r["attention"], [])

    def test_the_text_leads_with_attention_then_versions_then_the_table(self):
        out = fleetreport.render(self.report(dev("A", "a1", pending_changes=True)), STYLE)
        self.assertLess(out.index("Needs attention"), out.index("PAN-OS versions"))
        self.assertLess(out.index("PAN-OS versions"), out.index("Every firewall"))
        self.assertIn("nothing was changed", out)


if __name__ == "__main__":
    unittest.main()
