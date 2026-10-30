# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""`report` says what is wrong with the fleet before any upgrade is planned."""

import unittest

from panupgrade import console, fleetreport

STYLE = console.Style(False, False)


def dev(serial, name, version="10.2.1", peer="", **facts):
    return {"serial": serial, "hostname": name, "model": "M", "sw_version": version, "connected": True, "peer_serial": peer,
            "ha_state": facts.get("ha_state", ""), "facts": facts}


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

    def test_the_text_leads_with_attention_then_versions_then_the_table(self):
        out = fleetreport.render(self.report(dev("A", "a1", pending_changes=True)), STYLE)
        self.assertLess(out.index("Needs attention"), out.index("PAN-OS versions"))
        self.assertLess(out.index("PAN-OS versions"), out.index("Every firewall"))
        self.assertIn("nothing was changed", out)


if __name__ == "__main__":
    unittest.main()
