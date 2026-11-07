# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Terminal output: ANSI colour and emoji chosen by OUTCOME, with a plain fallback.

Every line carries a status glyph, so a long run can be scanned by shape and colour alone. The glyph is picked from what
happened (ok, failed, warned, skipped, waiting, held...), never from which step it was, so a step that merely reports
a normal condition does not look like an error. Without a colour terminal (pipe, ``NO_COLOR``, ``--color never``)
the same lines print as ``[ OK ]``-style tags so a saved log reads the same.
"""

import os
import shutil
import sys
import textwrap
import threading
import time

from . import progress, report

# outcome -> (emoji, plain tag, ANSI colour)
GLYPHS = {
    "start": ("\U0001F680", "[ GO ]", "1;36"),    # a phase begins (preflight, download, install, reboot...)
    "ok": ("✅", "[ OK ]", "32"),             # an outcome was confirmed
    "fail": ("❌", "[FAIL]", "1;31"),         # a step or device failed
    "warn": ("\U0001F7E7", "[WARN]", "38;5;208"),  # proceed, but a person should read this
    "skip": ("\U0001F7E8", "[SKIP]", "33"),       # deliberately not done
    "info": ("\U0001F4DD", "[INFO]", "36"),       # a fact worth recording (job ids, versions, counts)
    "wait": ("\U0001F527", "[ .. ]", "2"),        # a long operation is still running
    "stop": ("\U0001F6D1", "[STOP]", "1;37;41"),  # the run halted, or a human must look before it continues
    "sync": ("\U0001F504", "[SYNC]", "35"),       # configuration forced from the active member to the passive one
    "down": ("⏬", "[DOWN]", "1;35"),         # a downgrade
    "held": ("\U0001F512", "[HELD]", "33"),       # not started because its partner or an earlier device failed
}

VERDICT_GLYPH = {"ready": "ok", "caution": "warn", "blocked": "fail", "skipped": "skip"}
STATUS_GLYPH = {"pass": "ok", "warn": "warn", "fail": "fail", "error": "fail", "skip": "skip", "info": "info"}


def _enable_windows_vt():
    if os.name == "nt":
        os.system("")      # switches the console into VT (ANSI) mode; harmless if already on


class Style:
    def __init__(self, color=False, emoji=False, width=None):
        self.color, self.emoji = color, emoji
        self.width = width            # wrap long lines to this many columns (None: leave them alone, as in files and tests)

    @classmethod
    def detect(cls, mode="auto", ascii_only=False, stream=None):
        stream = stream or sys.stdout
        tty = hasattr(stream, "isatty") and stream.isatty()
        width = min(shutil.get_terminal_size((100, 24)).columns, 110) if tty else None
        if mode == "never" or os.environ.get("NO_COLOR"):
            return cls(False, False, width)
        if mode == "auto" and not tty:
            return cls(False, False)
        _enable_windows_vt()
        emoji = not ascii_only
        if emoji and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                emoji = False
        return cls(True, emoji, width)

    def paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color and code else text

    def bold(self, text):
        return self.paint(text, "1")

    def dim(self, text):
        return self.paint(text, "2")

    def glyph(self, kind):
        emoji, tag, code = GLYPHS[kind]
        if self.emoji:
            return emoji + " "
        return self.paint(tag, code) + " "

    def line(self, kind, text, indent=0):
        if kind in ("fail", "warn"):
            progress.finding(kind, text)              # as data too, for whoever is watching the work (the shell's whole-fleet entry)
        _, _, code = GLYPHS[kind]
        if self.width and len(text) + indent + 3 > self.width:
            text = textwrap.fill(text, max(self.width - indent - 3, 40), subsequent_indent=" " * (indent + 3), break_long_words=False)
            text = text.lstrip() if text.startswith(" ") else text
        return " " * indent + self.glyph(kind) + (self.paint(text, code) if kind in ("fail", "stop") else text)

    def bullet(self, text, indent=4):
        """A plain bullet line, wrapped under its own text."""
        prefix = " " * indent + "- "
        if self.width and len(prefix) + len(text) > self.width:
            return textwrap.fill(text, self.width, initial_indent=prefix, subsequent_indent=" " * len(prefix), break_long_words=False)
        return prefix + text


def render_plan(plan, style, verbose=False):
    s = plan["summary"]
    out = [style.bold("REPORT ONLY - nothing has been changed on any device"), "",
           f"Target {style.bold(plan['target'])}  |  Panorama {plan['panorama_version'] or 'unknown'}  |  "
           f"source {plan['source']}  |  plan {plan['hash'][:12]}",
           f"{s['devices']} devices: " + ", ".join([
               style.paint(f"{s['ready']} ready", GLYPHS["ok"][2]), style.paint(f"{s['caution']} need acknowledgement", GLYPHS["warn"][2]),
               style.paint(f"{s['blocked']} blocked", GLYPHS["fail"][2]), style.paint(f"{s['skipped']} not planned", GLYPHS["skip"][2])]),
           f"Traffic-affecting steps: {s['outage_steps']} outage, {s['failover_steps']} HA failover", ""]
    for w in plan["settings_warnings"]:
        out.append(style.line("warn", f"settings: {w}"))
    for r in plan["fleet_checks"]:
        if r["status"] != "pass":
            out.append(style.line(STATUS_GLYPH[r["status"]], f"fleet: {r['reason']}"))
    if report.gaps_line(plan):
        out.append(style.line("info", report.gaps_line(plan)))
    if plan["cautions"]:
        out += ["", style.bold("Cautions that need acknowledgement before an apply:")]
        out += [style.line("warn", f"[{c['code']}] {c['scope']}: {c['text']}", 2) for c in plan["cautions"]]
    if plan.get("advice"):
        out += ["", style.bold("Advice (never blocks an upgrade, nothing to acknowledge):")]
        for c in plan["advice"]:
            if c.get("devices"):
                out.append(style.line("info", f"{style.bold(c['title'])} ({len(c['devices'])})", 2))
                out.append(style.bullet(c["text"], 6))
                out += report.device_rows(c["devices"], "        ")
            else:
                out.append(style.line("info", f"[{c['code']}] {c['scope']}: {c['text']}", 2))
    for heading, devs in report.grouped_devices(plan):
        out += ["", style.bold(f"{heading} ({len(devs)})")]
        for dev in devs:
            role = dev.get("ha_state") or "standalone"
            head = f"{dev['hostname']} ({dev['serial']}) {role}  {dev['current_version']} -> {plan['target']}"
            head += f"  / {dev['skip_kind'].replace('_', ' ')}" if dev["skip_kind"] else ""
            out.append(style.line(VERDICT_GLYPH[dev["verdict"]], style.bold(head), 2))
            shown, note = report.findings(dev, verbose)
            out += [style.line(STATUS_GLYPH[r["status"]], f"{r['check']}: {r['reason']}", 6) for r in shown]
            if note:
                out.append(style.dim("      " + note))
    if plan["order"]:
        out += ["", style.bold("Steps")]
        for names_, steps, downgrade in report.step_groups(plan):
            out.append(style.line("start", style.bold(", ".join(names_)), 2))
            for st in steps:
                kind = "sync" if st["action"] == "sync to peer" else "down" if downgrade and st["action"].startswith("install") else "start"
                out.append("        " + style.glyph(kind) + style.dim(st["action"]))
    if plan["order"]:
        out += ["", style.bold(f"Order: each unit is worked in series (an HA pair, passive first); up to {plan['settings']['max_parallel']} units run at the same time")]
        names = {d["serial"]: d["hostname"] for d in plan["devices"]}
        for i, unit in enumerate(plan["order"], 1):
            out.append(f"  {i}. " + " > ".join(names[x] for x in unit))
        if plan.get("groups"):
            out += ["", style.bold(f"Stand-alone groups: at most {plan['settings']['group_reboot_tolerance']} member(s) of a group are out of service at the same time")]
            out += [f"  {name}: " + ", ".join(names[x] for x in members) for name, members in sorted(plan["groups"].items())]
    return "\n".join(out) + "\n"


def describe_event(record, names):
    """One journal record as (kind, text) for a log line, or None when it is not a step. ``kind`` is a glyph name (start, wait, sync, ok, fail, info)."""
    if record.get("type") != "step":
        return None
    step = record["step"].split(":", 1)[-1]
    state, detail = record["state"], record.get("detail") or {}
    if step == "baseline":
        return "info", f"session baseline {detail.get('sessions')} (from {names.get(detail.get('taken_from'), detail.get('taken_from'))})"
    if state == "started":
        kind = "start" if step.split()[0] in ("preflight", "download", "install", "reboot", "backup") else "wait"
        if step.startswith("sync"):
            kind = "sync"
        return kind, step
    if state == "ok":
        kind = "sync" if step.startswith("sync") else "ok"
        extra = ", ".join(f"{k}={v}" for k, v in detail.items() if k in ("version", "sessions_before", "sessions_after", "ha_state", "synced_from", "skipped", "config_sync", "peer_version", "original", "now"))
        return kind, (f"{step}  {extra}" if extra else step)
    if state == "failed":
        return "fail", f"{step}: {detail.get('error', '')}"
    if state == "note" and detail.get("message"):
        return "info", f"{step}: {detail['message']}"
    return "info", f"{step} {state}"


class EventPrinter:
    """Prints one line per journal event as the run happens: time, glyph, device, step, brief detail."""

    def __init__(self, style, names, out=None):
        self.style, self.names, self.out = style, names, out or sys.stdout
        self.width = max([len(n) for n in names.values()] + [8])

    def __call__(self, record):
        described = describe_event(record, self.names)
        if described is None:
            return
        kind, text = described
        stamp = time.strftime("%H:%M:%S", time.localtime())
        name = self.names.get(record["serial"], record["serial"]).ljust(self.width)
        print(f"{self.style.dim(stamp)}  {self.style.glyph(kind)}{self.style.bold(name)}  {text}", file=self.out, flush=True)


def render_outcomes(result, names, style, finished="finished all their steps"):
    mapping = {"done": "ok", "failed": "fail", "held": "held", "needs_attention": "stop", "stopped": "stop",
               "not_started": "skip", "not_approved": "skip"}
    lines = ["", style.bold("Result")]
    for serial, (state, detail) in result["outcome"].items():
        kind, parts = mapping.get(state, "info"), [p for p in (detail or "").split("; ") if p]
        if len(parts) <= 1 and len(detail or "") <= 70:
            lines.append(style.line(kind, f"{names.get(serial, serial)}: {state}" + (f" - {detail}" if detail else "")))
        else:                                                        # several things to say: one per line, not one long string
            lines.append(style.line(kind, f"{names.get(serial, serial)}: {state}"))
            lines += [style.bullet(p) for p in parts]
    done = sum(1 for state, _ in result["outcome"].values() if state == "done")
    upgraded = result.get("upgraded")
    verdict = (f"{done} of {len(result['outcome'])} firewalls {finished}"
               + (f", {len(upgraded)} are now on the target version" if upgraded is not None else "") + f", {result['failures']} failed")
    if result["stopped"]:
        verdict += ". Stopped early: the failure limit was reached, so nothing further was started"
    lines += ["", style.line("stop" if result["stopped"] else ("fail" if result["failures"] else "ok"), verdict)]
    lines += unrestored_lines(result.get("asymmetry_unrestored"), names, style)
    lines += lb_unrestored_lines(result.get("lb_unrestored"), names, style)
    return "\n".join(lines) + "\n"


def lb_unrestored_lines(unrestored, names, style):
    """The alert for members whose load-balancer drain (a protocol taken out of an interface management profile) could not be undone."""
    if not unrestored:
        return []
    lines = ["", style.line("fail", f"ALERT: {len(unrestored)} firewall(s) may still be drained from their load balancer: the interface management profile "
                                    f"was changed to drain them and was not confirmed back. Check the firewall and put the profile back by hand:")]
    for entry in unrestored:
        back = ", ".join(f"{p} {entry['original'][p] or '(not set)'}" for p in entry["changed"]) or "nothing was changed"
        lines.append(style.line("fail", f"{names.get(entry['serial'], entry['serial'])} ({entry['serial']}): profile {entry['profile']!r} should have {back}. "
                                        f"The tool's error: {entry['error']}"))
    return lines


def unrestored_lines(unrestored, names, style):
    """The alert for firewalls whose tcp-reject-non-syn / asymmetric-path were changed for an upgrade window and could not be put back."""
    if not unrestored:
        return []
    lines = ["", style.line("fail", f"ALERT: {len(unrestored)} firewall(s) are STILL set to accept asymmetric traffic. The tool changed tcp-reject-non-syn "
                                    f"and asymmetric-path for the upgrade window and could not put them back. Put them back by hand:")]
    for entry in unrestored:
        original = entry["original"]
        path = original["asymmetric_path"]
        def config(name, section, value):
            return (f"`set deviceconfig setting {section} {name} {value}`" if value else f"delete deviceconfig setting {section} {name} (it was not set)")
        fixes = (f"`set session tcp-reject-non-syn {'yes' if original['reject_non_syn'] else 'no'}`, "
                 + config("tcp-reject-non-syn", "session", original.get("reject_non_syn_config")) + ", and "
                 + config("asymmetric-path", "tcp", original.get("asymmetric_path")))
        lines.append(style.line("fail", f"{names.get(entry['serial'], entry['serial'])} ({entry['serial']}), {entry['window']}: {fixes}, then commit. "
                                        f"The tool's error: {entry['error']}"))
    return lines


def render_table(headers, rows, style):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    fmt = lambda row: "  ".join(str(c).ljust(w) for c, w in zip(row, widths))
    lines = [style.bold(fmt(headers)), style.dim("  ".join("-" * w for w in widths))]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines) + "\n"


class StatusBoard:
    """The view for parallel runs: one line per firewall, redrawn in place; failures scroll above it as ordinary log lines.

    It is just another listener on the journal, so it sees exactly the ordered stream everything else sees. Needs a colour
    terminal (it moves the cursor); otherwise the line-by-line EventPrinter is used.
    """

    def __init__(self, style, names, serials, out=None, refresh_s=1.0):
        self.style, self.names, self.out = style, names, out or sys.stdout
        self.serials = [s for s in serials if s in names]
        self.width = max([len(names[s]) for s in self.serials] + [8])
        self.state = {s: ("wait", "waiting", None) for s in self.serials}
        self._drawn, self._lock, self._stop = 0, threading.RLock(), threading.Event()
        self._thread = None
        if refresh_s:
            self._thread = threading.Thread(target=self._tick, args=(refresh_s,), daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._draw([])

    def _tick(self, every):
        while not self._stop.wait(every):
            with self._lock:
                self._draw([])

    def __call__(self, record):
        if record.get("type") != "step" or record["serial"] not in self.state:
            return
        step = record["step"].split(":", 1)[-1]
        detail = record.get("detail") or {}
        log = []
        with self._lock:
            kind = record["state"]
            if kind == "started":
                self.state[record["serial"]] = ("start", step, time.monotonic())
            elif kind == "ok":
                self.state[record["serial"]] = ("ok", f"{step} done", None)
            elif kind == "failed":
                self.state[record["serial"]] = ("fail", f"{step} FAILED", None)
                log.append(f"{self.style.dim(time.strftime('%H:%M:%S'))}  {self.style.glyph('fail')}"
                           f"{self.style.bold(self.names[record['serial']].ljust(self.width))}  {step}: {detail.get('error', '')}")
            self._draw(log)

    def _draw(self, log_lines):
        if self._drawn:
            self.out.write(f"\033[{self._drawn}A\033[J")
        for line in log_lines:
            self.out.write(line + "\n")
        now = time.monotonic()
        for serial in self.serials:
            kind, text, since = self.state[serial]
            elapsed = f"  {int(now - since)}s" if since else ""
            glyph = {"wait": "wait", "start": "start"}.get(kind, kind)
            self.out.write(f"{self.style.glyph(glyph)}{self.style.bold(self.names[serial].ljust(self.width))}  {text}{elapsed}\n")
        self._drawn = len(self.serials)
        self.out.flush()
