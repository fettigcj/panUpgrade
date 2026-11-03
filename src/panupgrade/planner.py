# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Turn a source's facts into a plan: what would happen to each device, in what order.

Read-only by construction: the planner only calls Source.list_devices() and
Source.facts(), and its output is data. Every device ends with a verdict and the
reasons behind it:

    ready     no problems found
    caution   plannable, but the user must acknowledge the listed cautions
    blocked   a check failed, errored, or a required check had no data
    skipped   not planned: not_applicable | no_data | excluded
"""

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__, checks, paths, progress
from .model import (ERROR, EXCLUDED, FAIL, INFO, NO_DATA, NOT_APPLICABLE, SAID_ONCE, SKIP, WARN, CheckResult)
from .sources import SourceError, read_facts
from .versions import Version, VersionError


class PlanError(ValueError):
    pass


def _glob_any(patterns, value):
    """True when ``value`` matches any pattern, ignoring case. A pattern with no * or ? is just an exact value."""
    value = (value or "").lower()
    return any(fnmatch.fnmatchcase(value, pattern.lower()) for pattern in patterns)


@dataclass
class Selection:
    serials: list = field(default_factory=list)
    name_glob: str = ""
    name_contains: str = ""
    models: list = field(default_factory=list)
    device_groups: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    version_is: str = ""           # glob on the running version, e.g. "10.2.*" or "10.2.1"
    version_older_than: str = ""
    version_newer_than: str = ""
    ha_states: list = field(default_factory=list)   # active | passive | ...
    accept_standalone_outage: bool = False

    def matches(self, ref):
        if self.serials and not _glob_any(self.serials, ref.serial):
            return False
        if self.name_glob and not fnmatch.fnmatch(ref.hostname.lower(), self.name_glob.lower()):
            return False
        if self.name_contains and self.name_contains.lower() not in ref.hostname.lower():
            return False
        if self.models and not _glob_any(self.models, ref.model):
            return False
        if self.device_groups and not _glob_any(self.device_groups, ref.device_group):
            return False
        if self.tags and not any(_glob_any(self.tags, tag) for tag in ref.tags):
            return False
        if self.ha_states and not _glob_any(self.ha_states, ref.ha_state):
            return False
        if self.version_is and not fnmatch.fnmatch(ref.sw_version or "", self.version_is):
            return False
        if self.version_older_than or self.version_newer_than:
            try:
                have = Version.parse(ref.sw_version)
                if self.version_older_than and not have < Version.parse(self.version_older_than):
                    return False
                if self.version_newer_than and not have > Version.parse(self.version_newer_than):
                    return False
            except VersionError:
                return False        # an unreadable version never matches a version criterion
        return True

    def describe(self):
        parts = []
        for label, value in (("serial in", self.serials), ("hostname like", self.name_glob), ("hostname contains", self.name_contains),
                             ("model in", self.models), ("device group in", self.device_groups), ("tag in", self.tags),
                             ("HA state in", self.ha_states), ("version is", self.version_is),
                             ("version older than", self.version_older_than), ("version newer than", self.version_newer_than)):
            if value:
                parts.append(f"{label} {value}")
        return "; ".join(parts) or "everything Panorama manages"


def build_plan(source, target_text, settings, selection=None, rules=None, now=None, releases=None):
    selection = selection or Selection()
    try:
        target = Version.parse(target_text)
    except VersionError as exc:
        raise PlanError(str(exc)) from None
    rules = rules if rules is not None else paths.load_rules()
    refs = source.list_devices()
    by_serial = {r.serial: r for r in refs}
    pano_version = source.panorama_version()

    devices = {}
    ctxs = {}
    wanted = sum(1 for r in refs if selection.matches(r))
    names = {r.serial: r.hostname for r in refs}
    read = read_facts(source, [r.serial for r in refs if selection.matches(r) and r.connected], settings.max_parallel,     # the slow part, side by side
                      lambda done, count, serial: progress.phase(f"Reading firewall {done} of {count}: {names[serial]}"))
    checked = 0
    for ref in refs:
        if not selection.matches(ref):
            devices[ref.serial] = _device(ref, "skipped", [CheckResult("selection", SKIP, "not in the selection", kind=EXCLUDED)],
                                          skip_kind=EXCLUDED)
            continue
        facts, error = None, ""
        checked += 1
        progress.phase(f"Checking firewall {checked} of {wanted}: {ref.hostname}")
        if ref.connected:
            if isinstance(read[ref.serial], SourceError):
                error = str(read[ref.serial])
            else:
                facts = read[ref.serial]
        peer = by_serial.get(getattr(ref, "peer_serial", None))
        ctx = checks.Context(ref, facts, target, settings, rules, pano_version, error, panorama_clock=source.panorama_clock(),
                             releases=releases, peer_version=peer.sw_version if peer else None)
        ctxs[ref.serial] = ctx
        results = checks.run_device_checks(ctx)
        verdict, kind = _verdict(results)
        devices[ref.serial] = _device(ref, verdict, results, skip_kind=kind, facts=facts)

    _apply_standalone(devices, ctxs, selection)
    _apply_pair_rules(devices, ctxs, by_serial, target)

    planned_versions = _planned_versions(devices, refs, target)
    fleet = checks.fleet_checks(pano_version, target, planned_versions, settings, releases)

    for dev in devices.values():
        if dev["verdict"] in ("ready", "caution"):
            dev["steps"], dev["rollback"] = _steps(dev, settings, devices)
    order = _order(devices, by_serial)

    cautions = _cautions(devices, fleet)
    plan = {
        "tool": "panupgrade", "tool_version": __version__,
        "mode": "REPORT ONLY - nothing has been changed on any device",
        "source": source.kind,
        "target": str(target),
        "panorama_version": pano_version,
        "settings": settings.__dict__.copy(),
        "settings_warnings": settings.loosening_warnings(),
        "selection": selection.__dict__.copy(),
        "fleet_checks": [r.to_dict() for r in fleet],
        "devices": list(devices.values()),
        "order": order,                              # the units (an HA pair in series, passive first; or one standalone), in the order they are started
        "cautions": cautions,
        "advice": _advice(devices, pano_version, releases),
        "summary": _summary(devices, fleet),
    }
    plan["content_hash"] = _hash(plan)
    plan["hash"] = _digest(plan)
    plan["snapshot"] = dict(_digest_body(plan), hostnames={d["serial"]: d["hostname"] for d in plan["devices"]})   # kept with an approval so a refusal can say what changed
    plan["created"] = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return plan


# ---------------------------------------------------------------- helpers

def _device(ref, verdict, results, skip_kind="", facts=None):
    return {
        "serial": ref.serial, "hostname": ref.hostname, "model": ref.model,
        "device_group": ref.device_group, "tags": sorted(ref.tags),
        "current_version": ref.sw_version,
        "ha_mode": facts.ha_mode if facts else None,
        "ha_state": facts.ha_state if facts else (ref.ha_state or None),
        "peer_serial": (facts.peer_serial if facts and facts.peer_serial else ref.peer_serial) or None,
        "preemption": facts.preemption if facts else None,
        "verdict": verdict, "skip_kind": skip_kind,
        "results": [r.to_dict() for r in results],
        "steps": [], "rollback": "",
    }


def _verdict(results):
    if results[0].status != "pass":
        return "skipped", NO_DATA
    for r in results:
        if r.status == SKIP and r.kind == NOT_APPLICABLE and r.check in ("current_version", "upgrade_path"):
            return "skipped", NOT_APPLICABLE
    if any(r.status in (FAIL, ERROR) for r in results):
        return "blocked", ""
    if any(r.status == SKIP and r.kind == NO_DATA and r.check in checks.REQUIRED for r in results):
        return "blocked", ""
    if any(r.ack for r in results):
        return "caution", ""
    return "ready", ""


def _add(dev, result):
    dev["results"].append(result.to_dict())
    dev["verdict"], dev["skip_kind"] = _verdict_from_dicts(dev)


def _verdict_from_dicts(dev):
    results = [CheckResult(r["check"], r["status"], r["reason"], r.get("kind", ""), r.get("ack", "")) for r in dev["results"]]
    return _verdict(results)


def _apply_standalone(devices, ctxs, selection):
    for serial, ctx in ctxs.items():
        dev = devices[serial]
        if dev["verdict"] not in ("ready", "caution") or ctx.facts.ha_mode != "none":
            continue
        if selection.accept_standalone_outage:
            _add(dev, CheckResult("standalone_outage", INFO, "standalone: outage during the reboot accepted by the user"))
        else:
            dev["results"].append(CheckResult("standalone_outage", SKIP,
                                              "standalone firewall: the reboot is an outage; select it with --accept-standalone-outage",
                                              kind=EXCLUDED).to_dict())
            dev["verdict"], dev["skip_kind"] = "skipped", EXCLUDED


def _apply_pair_rules(devices, ctxs, by_serial, target):
    plannable = {s for s, d in devices.items() if d["verdict"] in ("ready", "caution")}
    for serial in sorted(plannable):
        dev, ctx = devices[serial], ctxs[serial]
        if ctx.facts.ha_mode != "active-passive" or ctx.facts.ha_state not in ("active", "passive"):
            continue
        is_active = ctx.facts.ha_state == "active"
        peer = dev["peer_serial"]
        peer_ref = by_serial.get(peer)
        if peer_ref is None:
            code = "active_before_passive" if is_active else "lopsided_pair"
            _add(dev, CheckResult(code, WARN,
                                  "this is the " + ("active" if is_active else "passive") + " member and its HA peer is not visible in "
                                  "Panorama" + ("; the passive member cannot be upgraded first" if is_active else
                                                "; the pair will be left on different versions"), ack=code))
            continue
        try:
            peer_differs = Version.parse(peer_ref.sw_version) != target
        except VersionError:
            peer_differs = True
        if not peer_differs or peer in plannable:
            continue          # the peer is already on the target, or is in this run too
        problem = _peer_problem(devices.get(peer))
        if problem and ctx.settings.blocked_peer == "block":
            if is_active:
                said = ("it would hand the traffic to that peer while it reboots")
            else:
                said = ("upgrading only this member would leave the pair on two releases with its active peer unable to follow")
            _add(dev, CheckResult("peer_blocked", FAIL,
                                  f"held: its HA peer ({peer_ref.hostname}) is {problem}; {said}. Fix the peer and plan again, or set "
                                  f"blocked_peer to caution to go ahead anyway"))
            continue
        if is_active:
            _add(dev, CheckResult(
                "active_before_passive", WARN,
                f"this is the active member and its passive peer ({peer_ref.hostname}) is not on the target and "
                f"is not part of this plan. Best practice is to upgrade the passive first: it avoids an extra HA swap.",
                ack="active_before_passive"))
        else:
            _add(dev, CheckResult(
                "lopsided_pair", WARN,
                f"only this passive member is in the run; its active peer ({peer_ref.hostname}, {peer_ref.sw_version}) is not, "
                f"so the pair will run different versions until the peer is upgraded too (for example after you swap roles "
                f"by hand and run the tool again for the other half).",
                ack="lopsided_pair"))


def _peer_problem(peer):
    """Why an HA peer that is not in this run is a problem (words that fit "its peer is ..."), or "" when it is merely left out."""
    if peer is None:
        return ""
    if peer["verdict"] == "blocked":
        found = next((r for r in peer["results"] if r["status"] in (FAIL, ERROR)), None) or next(
            (r for r in peer["results"] if r["status"] == SKIP and r.get("kind") == NO_DATA), None)
        return "blocked" + (f" ({found['check']}: {found['reason']})" if found else "")
    if peer["verdict"] == "skipped" and peer["skip_kind"] == NO_DATA:
        found = next((r for r in peer["results"] if r["status"] == SKIP and r.get("kind") == NO_DATA), None)
        return "unreadable" + (f" ({found['reason']})" if found else "")
    return ""


def _planned_versions(devices, refs, target):
    out = []
    for ref in refs:
        if devices[ref.serial]["verdict"] in ("ready", "caution"):
            out.append(target)
            continue
        try:
            out.append(Version.parse(ref.sw_version))
        except VersionError:
            pass
    return out


def _path_steps(dev):
    for r in dev["results"]:
        if r["check"] == "upgrade_path" and "evidence" in r:
            return r["evidence"]["steps"]
    return []


def _steps(dev, settings, devices=None):
    role = "standalone" if dev["ha_mode"] == "none" else dev["ha_state"]
    hold = bool(dev["preemption"]) and role != "standalone"
    steps = []

    def add(action, traffic="none", note=""):
        steps.append({"action": action, "traffic": traffic, "note": note})

    syncing = any(r.get("ack") == "force_config_sync" for r in dev["results"])
    peer_planned = bool(devices and dev["peer_serial"] in devices
                        and devices[dev["peer_serial"]]["verdict"] in ("ready", "caution"))
    for p in _path_steps(dev):
        if p["download"]:
            add(f"download {p['version']}", note=f"image source: {settings.image_source}")
    add("backup", note="running config exported, parsed back and checksummed; device-state snapshot taken")
    if syncing and (role == "passive" or not peer_planned):
        add("sync to peer", "none", "after this member's own config is backed up, on the ACTIVE member: force its running configuration onto the passive member, then confirm "
                                     "HA1 config sync reads synchronized")
    if role == "active" and peer_planned:
        peer = devices[dev["peer_serial"]]["hostname"]
        add("wait for the passive firewall's upgrade to finish", note=f"this member does not start until {peer} (the passive member) has finished its own upgrade and checks; "
                                     f"its version is confirmed before this member hands over")
    if role == "active":
        takes_over = "its already upgraded passive peer takes over" if peer_planned else "its peer takes over"
        add("fail over", "failover",
            f"suspend this member so {takes_over}; compare the new active node's sessions with this one's, within {int(settings.failover_band * 100)}%; revert if out of band")
    if hold:
        add("hold suspended", note="preemption is on: keep this member suspended until go-live")
    installs = [p for p in _path_steps(dev) if p["install"]]
    for p in installs:
        traffic = "outage" if role == "standalone" else "redundancy"
        add(f"install {p['version']}", note=f"timeout {settings.install_timeout_s}s")
        add("reboot", traffic, f"timeout {settings.reboot_timeout_s}s")
        add("verify", note="version as expected, no jobs running")
    if hold or role == "active":
        add("go live", note="release the suspension so the member can rejoin the pair")
    if role != "standalone":
        add("verify HA", note="HA1 config sync, HA2 session sync, and (on the active node) sessions within "
            f"{int(settings.post_upgrade_band * 100)}% of the baseline")
    if role != "standalone" and settings.restore_roles == "restore" and role == "active":
        add("restore roles", "failover", "fail back so the original active member is active again")
    rollback = ("not automatic. The saved config and the previous image remain; a rollback is planned and approved "
                "separately. Exact commands to be verified from vendor documentation before implementation.")
    return steps, rollback


def _order(devices, by_serial):
    """Units in plan order: an HA pair is one unit (passive first), a standalone is one unit."""
    seen, units = set(), []
    for serial, dev in devices.items():
        if dev["verdict"] not in ("ready", "caution") or serial in seen:
            continue
        members = [serial]
        peer = dev["peer_serial"]
        if peer and peer in devices and devices[peer]["verdict"] in ("ready", "caution"):
            members.append(peer)
        members.sort(key=lambda s: (devices[s]["ha_state"] != "passive", s))
        seen.update(members)
        units.append(members)
    return units


def _cautions(devices, fleet):
    out = []
    for r in fleet:
        if r.ack:
            out.append({"scope": "fleet", "code": r.ack, "text": r.reason})
    once = {}
    for serial, dev in devices.items():
        if dev["verdict"] in ("ready", "caution"):
            for r in dev["results"]:
                if r.get("ack") in SAID_ONCE:
                    once.setdefault(r["ack"], {"text": r["reason"], "devices": []})["devices"].append(
                        {"serial": serial, "hostname": dev["hostname"], "version": dev["current_version"]})
                elif r.get("ack"):
                    out.append({"scope": serial, "code": r["ack"], "text": r["reason"]})
    out += [{"scope": "fleet", "code": code, "title": SAID_ONCE[code], "devices": found["devices"], "text": found["text"]}
            for code, found in once.items()]
    return out


def _advice(devices, panorama_version=None, table=None):
    """Notes that never need acknowledging and never hold an upgrade.

    Every firewall that lags behind Panorama is named in ONE note (serial, hostname, version), with the advice said once; the
    other advice is per firewall because it differs per firewall.
    """
    out = []
    lagging, evidence = [], {}
    for serial, dev in devices.items():
        for r in dev["results"]:
            if r["check"] == "panorama_range" and r["status"] == WARN and not r.get("ack"):
                lagging.append({"serial": serial, "hostname": dev["hostname"], "version": dev["current_version"]})
                evidence = r.get("evidence") or evidence
    if lagging:
        limit, unit = evidence.get("limit"), evidence.get("unit", "minor")
        stale = ""
        try:
            stale = checks._stale_note(table or checks.releases_mod.default(), Version.parse(panorama_version),
                                       *[Version.parse(d["version"]) for d in lagging])
        except (VersionError, TypeError):
            pass
        tail = (f" The target {evidence['target']} would still be {evidence['after']} {checks._unit_names(unit)} behind Panorama; "
                f"consider a newer target.") if evidence.get("after", 0) > (limit or 0) else ""
        out.append({"scope": "fleet", "code": "version_lagging", "title": checks.LAGGING_TITLE, "devices": lagging,
                    "text": checks.lagging_text(len(lagging), limit, unit, panorama_version)
                            + " Panorama must not be upgraded further while these lag." + tail + stale})
    for serial, dev in devices.items():
        out += [{"scope": serial, "code": "advise_upgrade", "text": r["reason"]}
                for r in dev["results"] if r["check"] in ("certificate_advisory", "device_certificate", "clock_skew")
                and r["status"] == WARN and not r.get("ack")]
    return out


def _summary(devices, fleet):
    counts = {"ready": 0, "caution": 0, "blocked": 0, "skipped": 0}
    kinds = {NOT_APPLICABLE: 0, NO_DATA: 0, EXCLUDED: 0}
    outage = failover = 0
    for dev in devices.values():
        counts[dev["verdict"]] += 1
        if dev["verdict"] == "skipped":
            kinds[dev["skip_kind"]] += 1
        for s in dev["steps"]:
            outage += s["traffic"] == "outage"
            failover += s["traffic"] == "failover"
    return {"devices": len(devices), **counts, "skipped_by_kind": kinds,
            "outage_steps": outage, "failover_steps": failover,
            "fleet_warnings": sum(1 for r in fleet if r.status == WARN)}


def _hash(plan):
    body = {k: v for k, v in plan.items() if k not in ("hash", "content_hash", "created", "snapshot")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _digest(plan):
    return hashlib.sha256(json.dumps(_digest_body(plan), sort_keys=True).encode()).hexdigest()


def _digest_body(plan):
    """What an approval is bound to: everything a person decides on, and nothing that merely drifts.

    The target, the settings, which firewalls will do what in which order (steps, HA roles, versions), and the codes of
    every caution. Only for the firewalls that would actually be upgraded: the others (already at the target, blocked, not selected)
    are not part of what is approved, and which of them were even read depends on how the run was started, so counting them made a
    prepared kit refuse itself. Not the explanatory text, free-disk or session-count numbers, job counts or timestamps: those change
    from minute to minute and would make an approval go stale for no reason a person cares about. If a firewall's HA role,
    version or planned steps change, or a caution appears or goes away, the digest changes and the approval is refused.
    """
    body = {
        "target": plan["target"], "panorama_version": plan["panorama_version"], "settings": plan["settings"],
        "devices": [{"serial": d["serial"], "version": d["current_version"], "ha_mode": d["ha_mode"], "ha_state": d["ha_state"],
                     "peer": d["peer_serial"], "verdict": d["verdict"],
                     "acks": sorted(r["ack"] for r in d["results"] if r.get("ack")),
                     "steps": [(s["action"], s["traffic"]) for s in d["steps"]]}
                    for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
        "order": plan["order"],
        "fleet_acks": sorted(r["ack"] for r in plan["fleet_checks"] if r.get("ack")),
    }
    return body
