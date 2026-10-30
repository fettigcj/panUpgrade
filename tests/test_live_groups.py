# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Device groups and tags come from where Panorama really keeps them."""

import unittest
import xml.etree.ElementTree as ET

from panupgrade_live import panorama

GROUPS = """<response status="success"><result><devicegroups>
<entry name="edge"><devices><entry name="S1"><serial>S1</serial><vsys><entry name="vsys1"/></vsys></entry><entry name="S2"/></devices></entry>
<entry name="core"><devices><entry name="S3"/></devices></entry>
<entry name="empty"/></devicegroups></result></response>"""


class DeviceGroups(unittest.TestCase):
    def test_each_serial_maps_to_its_group_and_vsys_entries_are_not_mistaken_for_firewalls(self):
        self.assertEqual(panorama.device_group_names(ET.fromstring(GROUPS)), {"S1": "edge", "S2": "edge", "S3": "core"})

    def test_a_reply_with_no_groups_gives_an_empty_map(self):
        self.assertEqual(panorama.device_group_names(ET.fromstring("<response><result/></response>")), {})

    def test_tags_are_read_when_set_and_empty_when_not(self):
        self.assertEqual(panorama.device_tags(ET.fromstring("<entry><tag><member>a</member><member> b </member></tag></entry>")), ["a", "b"])
        self.assertEqual(panorama.device_tags(ET.fromstring("<entry><tag /></entry>")), [])


if __name__ == "__main__":
    unittest.main()
