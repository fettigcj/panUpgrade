# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Render a plan as canonical JSON, readable text, and a self-contained HTML page.

The HTML uses panInventory's design tokens by name (the :root set only). It is a
stand-in until the shared panBrand asset set exists; then this stylesheet is
replaced by the copied, version-stamped file.
"""

import json
import textwrap
from html import escape

BANNER = "REPORT ONLY - nothing has been changed on any device"

_CHIP = {"ready": "pass", "caution": "warn", "blocked": "fail", "skipped": "skip",
         "pass": "pass", "warn": "warn", "fail": "fail", "skip": "skip", "info": "info", "error": "error"}


def to_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"


VERDICT_GROUPS = (("blocked", "Blocked: will not be upgraded until fixed"),
                  ("caution", "Need your acknowledgement"),
                  ("ready", "Ready"),
                  ("skipped", "Not planned"))


def grouped_devices(plan):
    """Devices under the verdict heading they belong to, worst first, so the reader meets problems before the routine."""
    return [(heading, [d for d in plan["devices"] if d["verdict"] == verdict]) for verdict, heading in VERDICT_GROUPS
            if any(d["verdict"] == verdict for d in plan["devices"])]


def findings(dev, verbose=False):
    """(results worth reading, one-line note about the rest).

    Skipped checks are routine on a firewall that will be upgraded (not applicable, nothing collected), so they are counted, not
    listed. On a blocked firewall a "no data" skip may be the very reason, so those stay. On a firewall that is not planned, the
    first skip of its kind is the explanation and the rest are counted.
    """
    results = [r for r in dev["results"] if r["status"] != "pass"]
    if verbose:
        return results, ""
    results = [r for r in results if not (r["check"] == "panorama_range" and r["status"] == "warn")]   # said once, for all of them, in the advice
    verdict = dev["verdict"]
    if verdict == "skipped":
        reason = next((r for r in results if r["status"] == "skip" and r.get("kind") == dev["skip_kind"]), None)
        keep = [r for r in results if r["status"] != "skip"] + ([reason] if reason else [])
    else:
        keep = [r for r in results if r["status"] != "skip" or (verdict == "blocked" and r.get("kind") == "no_data")]
    quiet = [r for r in results if r not in keep]
    if not quiet:
        return keep, ""
    return keep, f"+{len(quiet)} routine checks skipped"


def fleet_gaps(plan):
    """Checks that had no data on at least half the firewalls: said once for the fleet, not once per firewall."""
    counts = {}
    for dev in plan["devices"]:
        for r in dev["results"]:
            if r["status"] == "skip" and r.get("kind") == "no_data":
                counts[r["check"]] = counts.get(r["check"], 0) + 1
    return sorted(c for c, n in counts.items() if n * 2 >= len(plan["devices"]))


def gaps_line(plan):
    gaps = fleet_gaps(plan)
    return (f"No data was collected for {', '.join(gaps)} on most firewalls; those checks could not judge anything. "
            "Routine skipped checks are only counted per firewall; --verbose lists every one.") if gaps else ""


def step_groups(plan):
    """[(firewall names, steps, is a downgrade)]: firewalls with identical steps share one listing instead of repeating it."""
    groups = {}
    for dev in plan["devices"]:
        if dev["steps"]:
            downgrade = any(r.get("ack") == "downgrade" for r in dev["results"])
            key = json.dumps([[(st["action"], st["traffic"], st["note"]) for st in dev["steps"]], downgrade])
            groups.setdefault(key, ([], dev["steps"], downgrade))[0].append(dev["hostname"])
    return list(groups.values())


def _wrap(text, indent, width=100):
    return textwrap.fill(text, width=width, initial_indent=" " * indent, subsequent_indent=" " * (indent + 4))


def device_rows(devices, indent):
    """Firewalls in aligned columns: serial, hostname, PAN-OS version."""
    widths = [max(len(d[k]) for d in devices) for k in ("serial", "hostname")]
    return [f"{indent}{d['serial'].ljust(widths[0])}  {d['hostname'].ljust(widths[1])}  {d['version']}" for d in devices]


def to_text(plan, verbose=False):
    s = plan["summary"]
    lines = [BANNER, "",
             f"Target {plan['target']}  |  Panorama {plan['panorama_version'] or 'unknown'}  |  source {plan['source']}  |  plan {plan['hash'][:12]}",
             f"{s['devices']} devices: {s['ready']} ready, {s['caution']} need acknowledgement, {s['blocked']} blocked, {s['skipped']} not planned "
             f"(not applicable {s['skipped_by_kind']['not_applicable']}, no data {s['skipped_by_kind']['no_data']}, excluded {s['skipped_by_kind']['excluded']})",
             f"Traffic-affecting steps: {s['outage_steps']} outage, {s['failover_steps']} HA failover", ""]
    for w in plan["settings_warnings"]:
        lines.append(_wrap(f"CAUTION (settings): {w}", 0))
    if gaps_line(plan):
        lines.append(_wrap(gaps_line(plan), 0))
    for r in plan["fleet_checks"]:
        if r["status"] != "pass":
            lines.append(_wrap(f"FLEET {r['status'].upper()}: {r['reason']}", 0))
    if plan["cautions"]:
        lines += ["", "Cautions that need acknowledgement before an apply:"]
        lines += [_wrap(f"- [{c['code']}] {c['scope']}: {c['text']}", 2) for c in plan["cautions"]]
    if plan.get("advice"):
        lines += ["", "Advice (never blocks an upgrade, nothing to acknowledge):"]
        for c in plan["advice"]:
            if c.get("devices"):
                lines += [_wrap(f"- {c['title']} ({len(c['devices'])})", 2), _wrap(c["text"], 4)]
                lines += device_rows(c["devices"], "      ")
            else:
                lines.append(_wrap(f"- [{c['code']}] {c['scope']}: {c['text']}", 2))
    for heading, devs in grouped_devices(plan):
        lines += ["", f"{heading} ({len(devs)})"]
        for dev in devs:
            lines.append(f"  {dev['hostname']} ({dev['serial']}) {dev['model']} {dev['current_version']} -> {plan['target']}"
                         f"{' / ' + dev['skip_kind'].replace('_', ' ') if dev['skip_kind'] else ''}")
            shown, note = findings(dev, verbose)
            for r in shown:
                lines.append(_wrap(f"- {r['status']}{'/' + r['kind'] if r.get('kind') else ''} {r['check']}: {r['reason']}", 6))
            if note:
                lines.append(f"      ({note})")
    if plan["order"]:
        lines += ["", "Order (batches of at most max_parallel units; an HA pair is one unit, passive first)"]
        names = {d["serial"]: d["hostname"] for d in plan["devices"]}
        for i, batch in enumerate(plan["order"], 1):
            lines.append(f"  batch {i}: " + " | ".join(" > ".join(names[m] for m in unit) for unit in batch))
        lines += ["", "Steps"]
        for names_, steps, _ in step_groups(plan):
            lines.append(_wrap(", ".join(names_) + ":", 2))
            for st in steps:
                flag = f" [{st['traffic'].upper()}]" if st["traffic"] != "none" else ""
                lines.append(_wrap(f"- {st['action']}{flag}" + (f"  ({st['note']})" if st["note"] else ""), 4))
    return chr(10).join(lines) + chr(10)


_CSS = """:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#6b7686;--line:#e3e7ed;--accent:#2f6fed;
--fail:#d64545;--warn:#e0a020;--error:#8a4fd3;--pass:#2e9e5b;--skip:#98a2b3;--info:#3b8fc4}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1a2029;--ink:#e6eaf0;--mute:#8d99ab;--line:#2a323e;--accent:#6c9bff;
--fail:#ef6b6b;--warn:#e8b64a;--error:#b58af0;--pass:#4cc17f;--skip:#6f7a8c;--info:#5eb0e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1400px;margin:0 auto;padding:18px 20px 60px}h1{font-size:22px;margin:4px 0 14px}
h2{font-size:13px;margin:24px 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto;margin-bottom:12px}
.banner{border-color:var(--accent);font-weight:600}.caution{border-color:var(--warn)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;color:#fff;background:var(--skip)}
.chip.fail{background:var(--fail)}.chip.warn{background:var(--warn)}.chip.error{background:var(--error)}.chip.pass{background:var(--pass)}.chip.info{background:var(--info)}
.mute{color:var(--mute)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}"""


def _chip(word, label=None):
    return f'<span class="chip {_CHIP.get(word, "skip")}">{escape(label or word)}</span>'


def to_html(plan):
    s = plan["summary"]
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>panUpgrade plan</title><style>{_CSS}</style></head><body><main>",
           f'<h1>panUpgrade plan: target {escape(plan["target"])}</h1>',
           f'<div class="card banner">{escape(BANNER)}</div>',
           f'<div class="card">{s["devices"]} devices: {s["ready"]} ready, {s["caution"]} need acknowledgement, '
           f'{s["blocked"]} blocked, {s["skipped"]} not planned (not applicable {s["skipped_by_kind"]["not_applicable"]}, '
           f'no data {s["skipped_by_kind"]["no_data"]}, excluded {s["skipped_by_kind"]["excluded"]}).<br>'
           f'Traffic-affecting steps: {s["outage_steps"]} outage, {s["failover_steps"]} HA failover.<br>'
           f'<span class="mute">Panorama {escape(plan["panorama_version"] or "unknown")} - source {escape(plan["source"])} - plan '
           f'<span class="mono">{escape(plan["hash"][:12])}</span> - {escape(plan["created"])}</span></div>']
    notes = [("settings", w) for w in plan["settings_warnings"]] + \
            [("fleet", r["reason"]) for r in plan["fleet_checks"] if r["status"] != "pass"]
    for scope, text in notes:
        out.append(f'<div class="card caution">{_chip("warn", "caution")} {escape(scope)}: {escape(text)}</div>')
    if plan["cautions"]:
        out.append("<h2>Cautions needing acknowledgement</h2><div class=\"card\"><table><tr><th>Code</th><th>Scope</th><th>Why</th></tr>")
        for c in plan["cautions"]:
            out.append(f'<tr><td class="mono">{escape(c["code"])}</td><td class="mono">{escape(c["scope"])}</td><td>{escape(c["text"])}</td></tr>')
        out.append("</table></div>")
    out.append('<h2>Devices</h2><div class="card"><table><tr><th>Firewall</th><th>Model</th><th>Now</th><th>Verdict</th><th>Findings</th></tr>')
    for dev in plan["devices"]:
        label = dev["verdict"] + (f" / {dev['skip_kind'].replace('_', ' ')}" if dev["skip_kind"] else "")
        findings = "".join(
            f'<div>{_chip(r["status"])} <span class="mono">{escape(r["check"])}</span> {escape(r["reason"])}</div>'
            for r in dev["results"] if r["status"] != "pass")
        out.append(f'<tr><td>{escape(dev["hostname"])}<br><span class="mono mute">{escape(dev["serial"])}</span></td>'
                   f'<td>{escape(dev["model"])}</td><td class="mono">{escape(dev["current_version"])}</td>'
                   f'<td>{_chip(dev["verdict"], label)}</td><td>{findings}</td></tr>')
    out.append("</table></div>")
    if plan["order"]:
        names = {d["serial"]: d["hostname"] for d in plan["devices"]}
        out.append("<h2>Order and steps</h2>")
        for i, batch in enumerate(plan["order"], 1):
            out.append(f'<div class="card"><b>Batch {i}</b> <span class="mute">(an HA pair is one unit, passive first)</span>')
            for unit in batch:
                for member in unit:
                    dev = next(d for d in plan["devices"] if d["serial"] == member)
                    out.append(f'<h2>{escape(names[member])} <span class="mono">{escape(member)}</span></h2><table>')
                    for st in dev["steps"]:
                        flag = _chip("fail", st["traffic"]) if st["traffic"] in ("outage",) else (
                            _chip("warn", st["traffic"]) if st["traffic"] != "none" else "")
                        out.append(f'<tr><td>{escape(st["action"])} {flag}</td><td class="mute">{escape(st["note"])}</td></tr>')
                    out.append(f'</table><div class="mute">Rollback: {escape(dev["rollback"])}</div>')
            out.append("</div>")
    out.append("</main></body></html>")
    return "".join(out)
