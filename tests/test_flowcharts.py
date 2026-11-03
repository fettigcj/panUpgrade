# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The flow diagrams are generated from the source (tools/flowcharts.py); these tests keep the generator honest and the checks page current."""

import importlib.util
import pathlib
import unittest

import tests.support  # noqa: F401  (puts src on the path)

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("flowcharts", ROOT / "tools" / "flowcharts.py")
flowcharts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flowcharts)


class Flowcharts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = flowcharts.build_index()
        cls.docs = flowcharts.documents()

    def test_every_verb_of_the_command_line_has_a_chart(self):
        from panupgrade import cli
        parser = cli.build_parser()
        names = set(next(a for a in parser._actions if a.dest == "command").choices)
        self.assertEqual({v for v, _, _ in flowcharts.verbs(self.index)}, names)
        for verb in names:
            self.assertIn(f"## `panupgrade {verb}`", self.docs["verbs.md"])

    def test_every_device_check_is_on_the_checks_page_in_run_order(self):
        from panupgrade import checks
        page = self.docs["checks.md"]
        self.assertEqual([n for n, _ in flowcharts.device_checks(self.index)], [fn.__name__ for fn, _ in checks.DEVICE_CHECKS])
        chart = page.split("```mermaid")[1]
        positions = [chart.index(f"{fn.__name__}") for fn, _ in checks.DEVICE_CHECKS]
        self.assertEqual(positions, sorted(positions))

    def test_the_pages_say_which_verbs_run_the_checks(self):
        rows = {line.split("|")[1].strip(" `"): [c.strip() for c in line.split("|")[2:4]]
                for line in self.docs["checks.md"].splitlines() if line.startswith("| `") and line.count("|") == 4}
        self.assertEqual(rows["plan"], ["yes", "yes"])
        self.assertEqual(rows["report"][1], "no")                     # the fleet report reads every firewall but runs no checks
        self.assertEqual(rows["settings"], ["no", "no"])

    def test_the_checks_page_is_what_the_source_produces_now(self):
        committed = (ROOT / "docs" / "flow" / "checks.md").read_text(encoding="utf-8")
        self.assertEqual(committed, self.docs["checks.md"], "out of date: run python tools/flowcharts.py")

    def test_every_step_the_planner_can_give_is_carried_out_by_a_branch_of_do(self):
        branches =[test for test, _, _ in flowcharts.action_branches(flowcharts.find_function(self.index, "panupgrade.executor._do"))]
        for role, steps in flowcharts.steps_by_role():
            for step in steps:
                word = step.split(" ")[0] + " "
                self.assertTrue(any(f"== {step!r}" in b or f"({word!r}" in b or f"== \"{step}\"" in b for b in branches),
                                f"no branch of executor._do carries out {step!r} (for {role})")

    def test_the_fail_over_chart_suspends_then_compares_the_sessions_within_the_band(self):
        page = self.docs["upgrade.md"]
        section = page.split("### 'fail over'")[1].split("\n### ")[0]
        self.assertLess(section.index("backend.suspend(serial)"), section.index("_band_ok"))
        self.assertIn("settings.failover_band", section)
        self.assertIn("backend.functional(serial)", section)              # the revert when the band is missed

    def test_the_sync_step_checks_the_config_sync_after_it_asks_for_it(self):
        section = self.docs["upgrade.md"].split("### 'sync to peer'")[1].split("\n### ")[0]
        self.assertLess(section.index("backend.sync_to_peer(active)"), section.index("ha_config_sync"))

    def test_the_ha_decisions_table_covers_the_planning_and_the_upgrade(self):
        rows = flowcharts.ha_decisions(self.index)
        modules = {m for m, *_ in rows}
        self.assertTrue({"checks", "executor"} <= modules, modules)
        ha_peer = next(r for r in rows if r[1].qual == "ha_peer")
        self.assertIn("plan", ha_peer[4])                                  # reached through the list of checks, not by name

    def test_the_live_commands_behind_a_backend_call_are_listed(self):
        suspend = flowcharts.backend_commands(self.index, "suspend")
        self.assertIn(("sends", "<request><high-availability><state><suspend/></state></high-availability></request>"), suspend)
        self.assertTrue(any(kind == "waits" and "suspended" in text for kind, text in suspend))

    def test_a_call_the_code_makes_by_name_is_found(self):
        planner = self.index.funcs["panupgrade.planner.build_plan"]
        self.assertIn("panupgrade.checks.run_device_checks", [k for k, _ in self.index.calls(planner)])


if __name__ == "__main__":
    unittest.main()
