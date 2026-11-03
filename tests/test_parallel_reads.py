# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The fleet is read side by side (settings.max_parallel), and a firewall is read once however many places want it."""

import threading
import time
import unittest

import tests.support  # noqa: F401  (puts src on the path)
from panupgrade.sources import FakeFleet, SourceError, read_facts


class Slow:
    """A source whose reads take a moment and record how many were in flight at once."""

    def __init__(self, serials, fail=()):
        self.serials, self.fail, self.calls = serials, set(fail), []
        self.now = self.most = 0
        self.lock = threading.Lock()

    def facts(self, serial):
        with self.lock:
            self.calls.append(serial)
            self.now += 1
            self.most = max(self.most, self.now)
        time.sleep(0.05)
        with self.lock:
            self.now -= 1
        if serial in self.fail:
            raise SourceError("unreadable")
        return serial.upper()


class ReadFacts(unittest.TestCase):
    def test_reads_overlap_up_to_the_worker_count(self):
        src = Slow([f"S{i}" for i in range(8)])
        found = read_facts(src, src.serials, workers=4)
        self.assertEqual(found, {s: s.upper() for s in src.serials})
        self.assertTrue(2 <= src.most <= 4, src.most)

    def test_one_worker_reads_in_series_and_in_order(self):
        src = Slow(["A", "B", "C"])
        read_facts(src, src.serials, workers=1)
        self.assertEqual((src.most, src.calls), (1, ["A", "B", "C"]))

    def test_an_unreadable_firewall_comes_back_as_its_error_and_the_rest_are_read(self):
        src = Slow(["A", "B", "C"], fail=["B"])
        found = read_facts(src, src.serials, workers=3)
        self.assertIsInstance(found["B"], SourceError)
        self.assertEqual((found["A"], found["C"]), ("A", "C"))

    def test_a_serial_asked_for_twice_is_read_once(self):
        src = Slow(["A", "B"])
        read_facts(src, ["A", "B", "A"], workers=2)
        self.assertEqual(sorted(src.calls), ["A", "B"])

    def test_progress_is_announced_on_the_calling_thread(self):
        src, threads, seen = Slow(["A", "B", "C"]), set(), []
        read_facts(src, src.serials, workers=3, announce=lambda d, n, s: (threads.add(threading.current_thread()), seen.append((d, n))))
        self.assertEqual(threads, {threading.current_thread()})
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])


class AskedOnce(unittest.TestCase):
    def test_show_system_info_is_asked_once_per_firewall(self):
        from panupgrade import xmlops
        asked = []

        def op(command, **kw):
            asked.append(command)
            raise xmlops.XmlOpError("nothing here")
        xmlops.collect_facts(op)
        self.assertEqual(asked.count("show system info"), 1)       # a failure is not retried either: both readers see the same answer


class PrepareReadsEachFirewallOnce(unittest.TestCase):
    def test_a_pair_is_read_once_per_member_even_though_prepare_looks_at_the_peer(self):
        from panupgrade import paths, prepare, settings
        from panupgrade.planner import Selection
        from tests.support import fleet_data, pair

        reads = []

        class Counting(FakeFleet):
            def facts(self, serial):
                reads.append(serial)
                return super().facts(serial)
        prepare.build(Counting(fleet_data(*pair("A1", "A2"))), settings.load(), Selection(), "20.2.4", paths.load_rules())
        self.assertEqual(sorted(reads), ["A1", "A2"])


if __name__ == "__main__":
    unittest.main()
