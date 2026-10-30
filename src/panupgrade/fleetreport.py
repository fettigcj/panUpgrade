# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""``panupgrade report``: what state is the fleet in right now, before any upgrade is on the table.

Takes a snapshot (see ``sources.snapshot``) and answers three questions in this order: what needs attention, what is
running where, and what each firewall looks like. It reads only; it has no target version and plans nothing.
"""

from collections import Counter

from . import console


def _facts(dev):
    return dev.get("facts") or {}


def _pair_key(dev, by_serial):
    peer = dev.get("peer_serial")
    return tuple(sorted([dev["serial"], peer])) if peer and peer in by_serial else (dev["serial"],)


def _attention(devices):
    """[(hostname, [what is wrong, in plain words])] for firewalls that need a look, most worrying first."""
    by_serial = {d["serial"]: d for d in devices}
    found = {}

    def note(dev, text):
        found.setdefault(dev["hostname"], []).append(text)

    for dev in devices:
        facts = _facts(dev)
        if dev.get("collect_error"):
            note(dev, f"could not be read: {dev['collect_error']}")
        elif not dev.get("connected"):
            note(dev, "is not connected to Panorama")
            continue
        if facts.get("ha_state") in ("suspended", "non-functional", "tentative"):
            note(dev, f"HA state is {facts['ha_state']}")
        if facts.get("ha_config_sync") is False:
            note(dev, "HA config is not synchronized with its peer")
        compat = facts.get("ha_content_compat") or {}
        bad = sorted(k for k, v in compat.items() if v == "Mismatch")
        unknown = sorted(k for k, v in compat.items() if v == "Unknown")
        if bad:
            note(dev, f"content does not match its HA peer ({', '.join(bad)})")
        if unknown:
            note(dev, f"PAN-OS cannot tell whether content matches its HA peer ({', '.join(unknown)})")
        if facts.get("pending_changes"):
            note(dev, "has uncommitted configuration changes")
        if facts.get("running_jobs"):
            note(dev, f"has {facts['running_jobs']} job(s) running")
        if facts.get("licenses_ok") is False:
            note(dev, "has a license problem")
        if facts.get("ntp_synced") is False:
            note(dev, "clock is not synchronized with NTP")
    seen = set()
    for dev in devices:
        key = _pair_key(dev, by_serial)
        if len(key) == 2 and key not in seen:
            seen.add(key)
            a, b = (by_serial[s] for s in key)
            va, vb = _facts(a).get("sw_version") or a["sw_version"], _facts(b).get("sw_version") or b["sw_version"]
            if va != vb:
                note(a, f"HA pair runs different versions: {a['hostname']} {va}, {b['hostname']} {vb}")
            for kind in ("app", "threat"):
                ca, cb = (_facts(a).get("content_versions") or {}).get(kind), (_facts(b).get("content_versions") or {}).get(kind)
                if ca and cb and ca != cb:
                    note(a, f"HA pair has different {kind} content: {a['hostname']} {ca}, {b['hostname']} {cb}")
    return sorted(found.items())


def build(snap):
    devices = snap["devices"]
    by_serial = {d["serial"]: d for d in devices}
    versions = Counter((_facts(d).get("sw_version") or d["sw_version"] or "unknown") for d in devices)
    pairs = len({k for k in (_pair_key(d, by_serial) for d in devices) if len(k) == 2})
    standalone = sum(1 for d in devices if len(_pair_key(d, by_serial)) == 1)
    return {"panorama_version": (snap.get("panorama") or {}).get("version"),
            "devices": devices, "versions": dict(versions), "pairs": pairs, "standalone": standalone,
            "attention": _attention(devices)}


def _sync(facts):
    return {True: "yes", False: "NO", None: "-"}[facts.get("ha_config_sync")]


def render(report, style, address=""):
    devices = report["devices"]
    by_serial = {d["serial"]: d for d in devices}
    out = [style.bold(f"Fleet report{' for ' + address if address else ''}"),
           f"{len(devices)} firewall{'s' if len(devices) != 1 else ''}: {report['pairs']} HA pair{'s' if report['pairs'] != 1 else ''} "
           f"and {report['standalone']} standalone"
           f"  |  Panorama {report['panorama_version'] or 'unknown'}  |  nothing was changed", ""]
    if report["attention"]:
        out.append(style.bold(f"Needs attention ({len(report['attention'])} firewalls)"))
        for name, problems in report["attention"]:
            out.append(style.line("warn", style.bold(name), 2))
            out += [style.bullet(p, 6) for p in problems]
    else:
        out.append(style.line("ok", "Nothing needs attention."))
    out += ["", style.bold("PAN-OS versions")]
    for version, count in sorted(report["versions"].items(), key=lambda kv: kv[0]):
        out.append(f"  {version}  {count} firewall{'s' if count != 1 else ''}")
    rows = []
    for dev in sorted(devices, key=lambda d: (_pair_key(d, by_serial), d["hostname"])):
        facts = _facts(dev)
        cv = facts.get("content_versions") or {}
        rows.append([dev["hostname"], dev["model"], facts.get("sw_version") or dev["sw_version"],
                     facts.get("ha_state") or dev.get("ha_state") or "standalone", _sync(facts),
                     cv.get("app") or "-", cv.get("threat") or "-", "yes" if dev.get("connected") else "NO"])
    out += ["", style.bold("Every firewall, HA pairs together")]
    out.append(console.render_table(["hostname", "model", "PAN-OS", "HA", "cfg sync", "app content", "threat content", "online"], rows, style).rstrip("\n"))
    return "\n".join(out) + "\n"
