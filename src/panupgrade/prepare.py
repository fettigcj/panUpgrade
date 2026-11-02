# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Pre-maintenance task: smooth out what can be smoothed ahead of the upgrade window, so the window itself goes quicker.

What it does, for the selected firewalls:

* **Dynamic-content sync.** HA members should run the same cloud-delivered security content (applications/threats,
  antivirus, WildFire). With ``sync_content = prepare`` the latest is installed on a member that differs. This comes BEFORE
  the config sync and GATES it: if the members still differ afterwards, the config sync is not attempted.
* **Config sync.** With ``sync_to_peer = Both``, force each active member's running configuration onto a passive peer
  that reads "not synchronized". (A production active should always be syncing to its passive: failing over in an
  emergency must not change how the network behaves.) The passive member's own configuration is backed up and verified
  before it is overwritten.
* **Image pre-staging.** With a target version, download the image (and the base image a hop needs) onto every selected
  firewall that does not already have it, so the window does not spend its time on downloads.

It never installs, reboots, suspends or fails anything over. What it cannot fix (low disk, pending changes, unhealthy
HA) is left for ``plan`` to report.

Same discipline as an upgrade: a plan with a hash, an approval that names the firewalls, a journal that makes it
resumable, and no exception swallowed.
"""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import paths
from .executor import BackendError, ExecutionRefused
from .sources import SourceError
from .versions import Version, VersionError


class PrepareError(ValueError):
    pass


# "Check now" asks about every kind of update, as the GUI button does: a pair that has only ever asked about one kind has never learned the others
ALL_FAMILIES = ["content", "antivirus", "wildfire"]
# content key -> update family, for what must MATCH between HA members. WildFire is deliberately not here: it publishes a new release every
# few minutes, so two members one release apart is normal, and PAN-OS's own HA compatibility check has no WildFire field.
FAMILY = {"app": "content", "threat": "content", "av": "antivirus"}


def _norm(value):
    return None if value in (None, "", "0", "Unknown", "unknown") else value


def content_state(active, passive, passive_name):
    """What each pair has LOADED, in words. This is what `show system info` reports: it includes content built into the image, so it
    is NOT proof that an update package was ever installed (the GUI's Dynamic Updates page and PAN-OS's own HA verdict are the
    judges of that). 'Identical' means nothing when both are NONE, so say which is which."""
    if not active or passive is None:
        return "loaded content could not be read on one of the members"
    parts, none = [], []
    for key, label in (("app", "app"), ("threat", "threat"), ("av", "antivirus"), ("wildfire", "wildfire")):
        a, p = _norm(active.get(key)), _norm((passive or {}).get(key))
        if a and p and a == p:
            parts.append(f"{label} {a}")
        elif a or p:
            parts.append(f"{label} {a or 'NONE'} vs {p or 'NONE'} ({passive_name})")
        else:
            none.append(label)
    text = "loaded on both (as the firewall reports it; may be the image's built-in content, not an installed update): " + (
        ", ".join(parts) if parts else "nothing")
    if none:
        text += "; NONE loaded on either member: " + ", ".join(none)
    return text


def diff_text(diffs):
    return ", ".join(f"{k} {a or 'none'} vs {b or 'none'}" for k, (a, b) in sorted(diffs.items()))


def content_diffs(a, b):
    """{key: (a_value, b_value)} for each content type whose installed version differs between two members."""
    a, b = a or {}, b or {}
    return {k: (a.get(k), b.get(k)) for k in FAMILY if _norm(a.get(k)) != _norm(b.get(k))}


def build(source, settings, selection, target=None, rules=None):
    rules = rules if rules is not None else paths.load_rules()
    refs = {r.serial: r for r in source.list_devices()}
    want = Version.parse(target) if target else None
    units, downloads, skipped, notes, content_report, content_tasks = [], [], [], [], [], []
    for ref in refs.values():
        if not selection.matches(ref):
            continue
        if not ref.connected:
            skipped.append({"serial": ref.serial, "hostname": ref.hostname, "why": "not connected to Panorama"})
            continue
        try:
            facts = source.facts(ref.serial)
        except SourceError as exc:
            skipped.append({"serial": ref.serial, "hostname": ref.hostname, "why": f"facts unreadable: {exc}"})
            continue
        version = facts.sw_version or ref.sw_version
        steps = []
        if want is not None:
            try:
                result = paths.compute(version, str(want), rules, settings.allow_downgrade, settings.allow_cross_feature_downgrade)
            except VersionError as exc:
                skipped.append({"serial": ref.serial, "hostname": ref.hostname, "why": f"version unreadable: {exc}"})
                continue
            if not result.ok:
                skipped.append({"serial": ref.serial, "hostname": ref.hostname, "why": result.reason})
                continue
            steps = result.steps
            need, need_for = paths.content_requirement(rules, steps)
            try:
                app_now = int((facts.content_versions or {}).get("app", "0").split("-")[0])
            except (AttributeError, ValueError):
                app_now = 0
            if need is not None and app_now < need and settings.sync_content == "prepare":
                content_tasks.append({"serial": ref.serial, "hostname": ref.hostname, "needed": need, "for_version": need_for,
                                      "have": (facts.content_versions or {}).get("app") or "none"})
            have = set(facts.images_downloaded or [])
            for step in steps:
                if step["download"] and step["version"] not in have:
                    downloads.append({"serial": ref.serial, "hostname": ref.hostname, "version": step["version"]})
        # HA pairs: dynamic content and config, from the active member's point of view
        if facts.ha_mode == "active-passive" and facts.ha_state == "active":
            if want is not None and not steps:
                continue                      # this pair is not going anywhere, so leave it alone
            peer = facts.peer_serial or ref.peer_serial
            if not peer:
                if facts.ha_config_sync is False:
                    skipped.append({"serial": ref.serial, "hostname": ref.hostname, "why": "peer serial unknown"})
                continue
            try:
                peer_facts = source.facts(peer)
            except SourceError:
                peer_facts = None
            peer_version = (peer_facts.sw_version if peer_facts is not None and peer_facts.sw_version else None) or (refs[peer].sw_version if peer in refs else None)
            mixed = bool((facts.sw_version or ref.sw_version) and peer_version and (facts.sw_version or ref.sw_version) != peer_version)
            cfg_off = facts.ha_config_sync is False and not mixed      # members on different PAN-OS versions cannot sync configuration: not a task, not a note
            diffs = content_diffs(facts.content_versions, peer_facts.content_versions) if (
                peer_facts is not None and facts.content_versions and peer_facts.content_versions) else {}
            content_report.append({"serial": ref.serial, "hostname": ref.hostname, "text": content_state(
                facts.content_versions, peer_facts.content_versions if peer_facts else None, refs[peer].hostname if peer in refs else peer)})
            families = sorted({FAMILY[k] for k in diffs})
            compat = facts.ha_content_compat or {}
            bad = sorted(k for k in ("app", "threat", "av") if compat.get(k) not in (None, "Match"))
            reason = "installed versions differ" if diffs else ""
            if cfg_off and bad and not families and _norm((facts.content_versions or {}).get("app")):
                families = ["content"]            # same versions, but PAN-OS itself cannot confirm the pair's content is compatible
                reason = "PAN-OS reports the HA content compatibility as " + ", ".join(f"{k}: {compat[k]}" for k in bad)
            need_content = bool(families) and settings.sync_content == "prepare"
            need_cfg = cfg_off and settings.sync_to_peer == "Both"
            if families and not need_content:
                notes.append({"serial": ref.serial, "hostname": ref.hostname,
                              "why": f"dynamic content is not level between the HA members ({reason}); "
                                     f"sync_content is 'never', so prepare leaves it and will not attempt a config sync"})
            if cfg_off and not need_cfg:
                notes.append({"serial": ref.serial, "hostname": ref.hostname,
                              "why": ("config is not synchronized with the passive member; sync_to_peer is "
                                      f"'{settings.sync_to_peer}', so prepare leaves it" +
                                      (" (the upgrade run will sync it)" if settings.sync_to_peer == "UpgradeOnly"
                                       else " (set sync_to_peer to Both or UpgradeOnly to have the tool fix it)"))})
            if need_content or need_cfg:
                units.append({"active": ref.serial, "active_name": ref.hostname, "passive": peer,
                              "passive_name": refs[peer].hostname if peer in refs else peer,
                              "sync_config": need_cfg, "content": families if need_content else [],
                              "content_diffs": {k: list(v) for k, v in diffs.items()}, "content_reason": reason})
    in_units = {s for u in units if u["content"] for s in (u["active"], u["passive"])}
    content_tasks = [t for t in content_tasks if t["serial"] not in in_units]        # a pair's own content step already covers its members
    serials = sorted({u["active"] for u in units} | {d["serial"] for d in downloads} | {t["serial"] for t in content_tasks})
    plan = {"kind": "prepare", "target": target, "mode": "nothing changed until an approved `prepare` run",
            "units": units, "downloads": downloads, "content_tasks": content_tasks, "serials": serials, "skipped": skipped, "notes": notes,
            "content_report": content_report}
    plan["hash"] = hashlib.sha256(json.dumps({"units": units, "downloads": downloads, "content_tasks": content_tasks, "target": target},
                                             sort_keys=True).encode()).hexdigest()
    return plan


def work_groups(plan):
    """What the screen sorts a run into: one entry per HA pair (its two members) or standalone firewall, each member a dict with its
    serial, hostname and the (journal) step names this prepare will finish for it. The same grouping the run itself works in."""
    groups = {}

    def member(key, serial, hostname):
        group = groups.setdefault(key, {})
        return group.setdefault(serial, {"serial": serial, "hostname": hostname, "expected": []})

    for unit in plan["units"]:
        key = frozenset({unit["active"], unit["passive"]})
        member(key, unit["passive"], unit["passive_name"])
        member(key, unit["active"], unit["active_name"])["expected"].append("sync to peer" if unit["sync_config"] else "content check")
    partner = {s: frozenset({u["active"], u["passive"]}) for u in plan["units"] for s in (u["active"], u["passive"])}
    for task in plan.get("content_tasks", []):
        member(partner.get(task["serial"], frozenset({task["serial"]})), task["serial"], task["hostname"])["expected"].append(
            "update content (minimum for the target)")
    for task in plan["downloads"]:
        member(partner.get(task["serial"], frozenset({task["serial"]})), task["serial"], task["hostname"])["expected"].append(f"download {task['version']}")
    return [list(group.values()) for group in groups.values()]


def template(plan):
    return {"plan_hash": plan["hash"], "serials": [], "acks": [], "approved_by": "", "note": "",
            "cautions_available": [], "affected_firewalls": plan["serials"]}


def callout(plan):
    """What a person needs to read before allowing content changes: which pairs, why, and that it is out of band."""
    lines = []
    for u in plan["units"]:
        if u["content"]:
            why = u.get("content_reason") or "installed versions differ"
            extra = f" ({diff_text(u['content_diffs'])})" if u["content_diffs"] else ""
            lines.append(f"{u['active_name']} + {u['passive_name']}: {why}{extra} -> install the latest {' and '.join(u['content'])} update")
    return lines


def describe(plan):
    """One line per task, for the screen and for the approval prompt."""
    lines = []
    for u in plan["units"]:
        if u["content"]:
            lines.append(f"{u['active_name']} + {u['passive_name']}: 'check now' on both members, then (only if PAN-OS still cannot confirm) install the latest {' and '.join(u['content'])} update on both "
                         f"({u.get('content_reason') or 'versions differ'}{': ' + diff_text(u['content_diffs']) if u['content_diffs'] else ''})")
        if u["sync_config"]:
            lines.append(f"{u['active_name']} (active) -> {u['passive_name']} (passive): force the active member's configuration "
                         f"onto the passive, once the content versions match")
    lines += [f"{t['hostname']}: install the latest dynamic content (has {t['have']}; {t['for_version']} needs {t['needed']} or newer, or PAN-OS refuses the install)"
              for t in plan.get("content_tasks", [])]
    lines += [f"{d['hostname']}: download {d['version']} (does not install it)" for d in plan["downloads"]]
    return lines


def _fold(parts):
    states = [s for s, _ in parts]
    detail = "; ".join(d for _, d in parts if d)
    for worst in ("failed", "needs_attention"):
        if worst in states:
            return worst, "; ".join(d for s, d in parts if s == worst and d)         # what went wrong, without the successes around it
    if set(states) == {"not_approved"}:
        return "not_approved", detail
    if "not_started" in states and "done" not in states:
        return "not_started", detail
    return "done", detail


def run(plan, approval, backend, journal, settings=None, stop=None):
    max_failures = settings.max_failures if settings else 5
    problems = []
    if approval.plan_hash != plan["hash"]:
        problems.append("the approval is for a different prepare plan (hash mismatch)")
    if journal.plan_hash != plan["hash"]:
        problems.append("the journal belongs to a different plan")
    if not set(plan["serials"]) & set(approval.serials):
        problems.append("the approval names none of the firewalls in this plan")
    if problems:
        raise ExecutionRefused(problems)

    parts, failed = {}, set()
    lock = threading.Lock()
    stop = stop or threading.Event()

    def note(serial, state_, detail):
        with lock:
            parts.setdefault(serial, []).append((state_, detail))

    def attempt(serial, step, detail_ok, fn, record_serial=None):
        """One journaled task. Returns False if it failed or needs attention."""
        who = record_serial or serial
        if serial not in approval.serials:
            note(serial, "not_approved", "not named in the approval")
            return False
        if journal.last_state(who, step) == "ok":
            note(serial, "done", f"{step}: already done in this journal")
            return True
        if journal.last_state(who, step) == "started":
            note(serial, "needs_attention", f"{step} was started and not finished; check the device before resuming")
            return False
        with lock:
            over = len(failed) >= max_failures
            count = len(failed)
        if over or stop.is_set():
            note(serial, "not_started", f"stopped after {count} failed task(s)" if over else "stopped by you before it started")
            return False
        try:
            journal.record(who, step, "started")
            detail = fn() or {}
            journal.record(who, step, "ok", detail)
            note(serial, "done", detail_ok)
            return True
        except Exception as exc:     # nothing a device does may pass unrecorded
            journal.record(who, step, "failed", {"error": str(exc), "type": type(exc).__name__})
            note(serial, "failed", f"{step}: {exc}")
            with lock:
                failed.add(serial)
            return False

    def do_unit(unit):
        active, passive = unit["active"], unit["passive"]

        def backup_passive():
            saved = backend.backup(passive)
            if not saved.get("parsed_ok") or not saved.get("sha256"):
                raise BackendError(f"backup of {passive} was not verified: {saved!r}")
            return {"sha256": saved["sha256"], "bytes": saved.get("bytes")}

        def content_match():
            a, b = backend.content_versions(active), backend.content_versions(passive)
            diffs = content_diffs(a, b)
            compat = backend.state(active).get("content_compat") or {}
            bad = sorted(k for k in ("app", "threat", "av") if compat.get(k) not in (None, "Match"))
            if diffs or bad:
                raise BackendError("dynamic content is not level between the members (" + "; ".join(
                    x for x in (diff_text(diffs), ("PAN-OS reports HA content compatibility " + ", ".join(f"{k}: {compat[k]}" for k in bad)) if bad else "") if x) +
                                   "); the config sync is NOT attempted until it is")
            return {"app": a.get("app"), "threat": a.get("threat"), "av": a.get("av"), "wildfire": a.get("wildfire")}

        def sync():
            if backend.state(active)["ha_state"] != "active":
                raise BackendError(f"{active} is not the active member any more")
            backend.sync_to_peer(active)
            now = backend.state(passive)["ha_config_sync"]
            if now is not True:
                raise BackendError(f"after the sync, HA1 config sync reads {now!r}")
            return {"synced_from": active, "to": passive}

        if active not in approval.serials:
            note(active, "not_approved", "not named in the approval")
            return
        ok = True
        settled = False
        if unit["content"]:
            # 1a. "check now" on both members first: on a pair that has never asked the update server, this alone can make PAN-OS
            #     recognise the content it already has. Read-only on the firewalls.
            for member, label in ((passive, unit["passive_name"]), (active, unit["active_name"])):
                ok = attempt(active, "check now", f"{label}: update lists refreshed",
                             lambda m=member: backend.refresh_updates(m, ALL_FAMILIES), record_serial=member) and ok
            # 1b. give PAN-OS's own HA verdict a little while to catch up; if it does, nothing needs installing
            if ok:
                found = {}

                def settle():
                    deadline = time.monotonic() + (settings.content_settle_s if settings else 0)
                    while True:
                        a, b = backend.content_versions(active), backend.content_versions(passive)
                        compat = backend.state(active).get("content_compat") or {}
                        if not content_diffs(a, b) and not [k for k in ("app", "threat", "av") if compat.get(k) not in (None, "Match")]:
                            found["settled"] = True
                            return {"settled": True}
                        if time.monotonic() >= deadline:
                            found["settled"] = False
                            return {"settled": False, "compat": compat}
                        time.sleep(settings.poll_interval_s if settings else 1)
                ok = attempt(active, "recheck compatibility", "", settle)
                settled = bool(found.get("settled"))
        if ok and not settled:
            for family in unit["content"]:                  # 1c. otherwise bring content level (passive first, then active)
                for member, label in ((passive, unit["passive_name"]), (active, unit["active_name"])):
                    ok = attempt(active, f"update {family}", f"{label}: {family} up to date",
                                 lambda m=member, f=family: backend.update_content(m, f), record_serial=member) and ok
        if ok and (unit["content"] or unit["sync_config"]):  # 2. the gate: the members must now match
            ok = attempt(active, "content check", "content versions match" if unit["content"] else "", content_match)
        if ok and unit["sync_config"]:                       # 3. only then the config sync
            if attempt(active, "backup before sync", "", backup_passive, record_serial=passive):
                attempt(active, "sync to peer", f"synced {unit['active_name']} -> {unit['passive_name']}", sync)

    def do_download(task):
        version = task["version"]
        attempt(task["serial"], f"download {version}", f"downloaded {version}",
                lambda s=task["serial"], v=version: backend.download(s, v))

    # Work is grouped so that nothing ever touches the same pair from two threads: a pair's content/config tasks run first,
    # then the downloads for its members; different pairs (and standalone firewalls) run side by side.
    partner = {}
    for unit in plan["units"]:
        partner[unit["active"]] = partner[unit["passive"]] = frozenset({unit["active"], unit["passive"]})
    groups = {}
    blank = lambda: {"units": [], "downloads": [], "content_tasks": []}
    for unit in plan["units"]:
        groups.setdefault(partner[unit["active"]], blank())["units"].append(unit)
    for task in plan.get("content_tasks", []):
        groups.setdefault(partner.get(task["serial"], frozenset({task["serial"]})), blank())["content_tasks"].append(task)
    for task in plan["downloads"]:
        groups.setdefault(partner.get(task["serial"], frozenset({task["serial"]})), blank())["downloads"].append(task)

    def do_content_task(task):
        serial = task["serial"]

        def install_and_check():
            backend.refresh_updates(serial, ["content"])
            detail = backend.update_content(serial, "content") or {}
            try:
                now = int((backend.content_versions(serial).get("app") or "0").split("-")[0])
            except (AttributeError, ValueError):
                now = 0
            if now < task["needed"]:
                raise BackendError(f"after the update the firewall has content {now}, still below the {task['needed']} that "
                                   f"{task['for_version']} needs")
            return detail
        attempt(serial, "update content (minimum for the target)", "content meets the minimum for the target", install_and_check)

    def do_group(group):
        for unit in group["units"]:
            do_unit(unit)
        for task in group["content_tasks"]:
            do_content_task(task)
        for task in group["downloads"]:
            do_download(task)

    pool = ThreadPoolExecutor(max_workers=max(1, settings.max_parallel if settings else 1), thread_name_prefix="prep")
    futures = [pool.submit(do_group, g) for g in groups.values()]
    try:
        for future in futures:
            future.result()
    except BaseException:           # Ctrl-C or a bug: no new tasks; tasks already running finish
        stop.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True)
        raise
    pool.shutdown()

    outcome = {serial: _fold(p) for serial, p in parts.items()}
    return {"outcome": outcome, "failures": len(failed), "stopped": len(failed) >= max_failures}
