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
import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__, checks, paths, progress, xmlops
from .model import (WAIT_FIRST, WAIT_PASSIVE, ERROR, EXCLUDED, FAIL, INFO, NO_DATA, NOT_APPLICABLE, SAID_ONCE, SKIP, WARN, CheckResult)
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
    name_regex: str = ""           # a regular expression searched for anywhere in the host name (^ and $ anchor it), ignoring case
    models: list = field(default_factory=list)
    device_groups: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    version_is: str = ""           # glob on the running version, e.g. "10.2.*" or "10.2.1"
    version_older_than: str = ""
    version_newer_than: str = ""
    ha_states: list = field(default_factory=list)   # active | passive | ...
    ha_states_is: list = field(default_factory=list)         # the same, from the form's "is" mode (no wildcard is added for the reader)
    device_groups_is: list = field(default_factory=list)
    models_is: list = field(default_factory=list)
    accept_standalone_outage: bool = False
    groups: dict = field(default_factory=dict)      # serial -> stand-alone group name (from the firewall list file: 'SERIAL, group')

    def matches(self, ref):
        if self.serials and not _glob_any(self.serials, ref.serial):
            return False
        if self.name_glob and not fnmatch.fnmatch(ref.hostname.lower(), self.name_glob.lower()):
            return False
        if self.name_contains and self.name_contains.lower() not in ref.hostname.lower():
            return False
        if self.name_regex:
            try:
                if not re.search(self.name_regex, ref.hostname, re.IGNORECASE):
                    return False
            except re.error:
                return False        # an expression that does not compile matches nothing (the form and the command line say why)
        if self.models and not _glob_any(self.models, ref.model):
            return False
        if self.models_is and not _glob_any(self.models_is, ref.model):
            return False
        if self.device_groups and not _glob_any(self.device_groups, ref.device_group):
            return False
        if self.device_groups_is and not _glob_any(self.device_groups_is, ref.device_group):
            return False
        if self.tags and not any(_glob_any(self.tags, tag) for tag in ref.tags):
            return False
        if self.ha_states and not _glob_any(self.ha_states, ref.ha_state):
            return False
        if self.ha_states_is and not _glob_any(self.ha_states_is, ref.ha_state):
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

    @staticmethod
    def regex_problem(text):
        """Why a regular expression cannot be used, in words, or "" when it is fine."""
        try:
            re.compile(text)
        except re.error as exc:
            return f"not a valid regular expression: {exc}"
        return ""

    def describe(self):
        parts = []
        for label, value in (("serial in", self.serials), ("hostname like", self.name_glob), ("hostname contains", self.name_contains), ("hostname matches regex", self.name_regex),
                             ("model in", self.models), ("model is", self.models_is), ("device group in", self.device_groups), ("device group is", self.device_groups_is), ("tag in", self.tags),
                             ("HA state in", self.ha_states), ("HA state is", self.ha_states_is), ("version is", self.version_is),
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

    sizes = {}
    for name in selection.groups.values():
        sizes[name] = sizes.get(name, 0) + 1
    for serial, name in selection.groups.items():
        seen = ctxs[serial].facts if serial in ctxs else None
        ha_member = bool(seen and seen.ha_mode not in (None, "none"))       # a firewall in an HA pair is refused a group (below), however many are named
        if serial in devices and sizes[name] < 2 and not ha_member:           # a group of one is no group: it is treated exactly like any other stand-alone firewall
            _add(devices[serial], CheckResult("standalone_group", INFO, f"it is the only firewall named in the group {name!r}, so it is treated as a "
                                                                        f"stand-alone firewall, not as a group member"))
        elif serial in devices:
            devices[serial]["group"] = name
            devices[serial]["group_peers"] = sorted(s for s, other in selection.groups.items() if other == name and s != serial)
    _apply_pending(devices, ctxs, by_serial, selection, target)
    _apply_standalone(devices, ctxs, selection)
    _apply_plugins(devices, ctxs, settings)
    _apply_pair_rules(devices, ctxs, by_serial, target)
    _apply_positions(devices, settings)

    planned_versions = _planned_versions(devices, refs, target)
    fleet = checks.fleet_checks(pano_version, target, planned_versions, settings, releases)

    for dev in devices.values():
        if dev["verdict"] in ("ready", "caution"):
            dev["steps"], dev["rollback"] = _steps(dev, settings, devices)
    order = _order(devices, by_serial, settings)

    groups = _groups_planned(devices)
    windows = _asymmetry_windows(devices, order, groups, settings, selection, by_serial)
    cautions = _cautions(devices, fleet)
    placed = sorted(d["hostname"] for d in devices.values() if d.get("position") == "first" and settings.upgrade_first == "active")
    if placed:
        cautions.append({"scope": "fleet", "code": "upgrade_first_active", "text": (
            f"upgrade_first is active: in each of these pairs ({', '.join(placed)}) the ACTIVE member is suspended and upgraded first, so its passive "
            f"peer, still on the old version, takes the traffic; the session count must come within {int(settings.pair_session_warn_band * 100)}% of before (a warning if still short after "
            f"{settings.pair_session_wait_s}s, a failure at {int(settings.pair_session_fail_band * 100)}% or more short, when the member is "
            f"put back). The peer is then suspended in turn so the upgraded member takes the traffic back before the peer is upgraded. That is two "
            f"HA fail-overs per pair instead of one.")})
    if windows:
        cautions.append({"scope": "fleet", "code": "asymmetry_window", "text": _window_words(windows)})
    if groups and settings.lb_drain_profile:
        cautions.append({"scope": "fleet", "code": "lb_drain", "text": (
            f"Each member of these stand-alone groups ({', '.join(sorted(groups))}) is drained from its load balancer before it reboots: "
            f"{settings.lb_drain_probe_protocol} is taken out of the interface management profile {settings.lb_drain_profile!r} (committed on the firewall, "
            f"which affects every interface that uses the profile), the tool waits {settings.lb_drain_wait_s}s, and the profile is put back after the member "
            f"is upgraded, or after it failed. A firewall whose profile could not be put back is reported at the end and needs a person.")})
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
        "groups": groups,                            # stand-alone group name -> the serials planned in it (each reboots at most group_install_tolerance at a time)
        "asymmetry": windows,                        # where the tool will change tcp-reject-non-syn and asymmetric-path, and put them back
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
        "steps": [], "rollback": "", "group": "",
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


def _apply_positions(devices, settings):
    """For a pair with both members in the run, say which goes first and which second. Passive first (the default): the passive member, then the
    active one. Active first: the active member, then the one that was passive. A pair with a member missing, or a suspended member, keeps the
    old order (the suspended member goes first, the active one last) and has no position."""
    active_first = settings.upgrade_first == "active"
    for serial, dev in devices.items():
        if dev["ha_mode"] != "active-passive" or dev["ha_state"] != "active" or dev["verdict"] not in ("ready", "caution"):
            continue
        other = devices.get(dev["peer_serial"])
        if other is None or other["verdict"] not in ("ready", "caution") or other["ha_state"] != "passive":
            continue
        dev["position"], other["position"] = ("first", "second") if active_first else ("second", "first")


def _swap(dev, result):
    """Replace the recorded result of the same check, then judge the firewall again."""
    dev["results"] = [r for r in dev["results"] if r["check"] != result.check] + [result.to_dict()]
    dev["verdict"], dev["skip_kind"] = _verdict_from_dicts(dev)


def _apply_pending(devices, ctxs, by_serial, selection, target):
    """An image that is installed and not booted holds a firewall (it refuses every commit). Three cases are understood; anything else is
    left to the user to clean up first:
    * the pending image IS the target: the upgrade only needs the reboot ("finish");
    * the pending image is what the firewall's HA peer, or every other member of its group, already runs: reboot into it so the pair or
      group is on one version, then go on to the target ("match");
    * anything else: blocked, with what to run first."""
    for serial, ctx in ctxs.items():
        dev, facts = devices[serial], ctx.facts
        if facts is None or facts.pending_reboot is not True or dev["verdict"] in ("skipped",):
            continue
        shown = facts.pending_reboot_version or "an image"
        try:
            pend = Version.parse(facts.pending_reboot_version)
        except VersionError:
            pend = None
        clean_up = (f"{shown} is installed and waiting for a reboot, and the plan cannot tell whether booting it is wanted. Run a clean-up batch first "
                    f"(reboot it into {shown}, or install the version you want and reboot) so this firewall starts from a clean slate, then plan again")
        if pend is None:
            _swap(dev, CheckResult("pending_reboot", FAIL, clean_up))
            continue
        case, others = "", []
        if pend == target:
            case = "finish"
        elif ctx.facts.ha_mode in ("active-passive", "active-active"):
            others = [ctx.peer_version]
        elif dev["group"]:
            others = [by_serial[s].sw_version for s, name in selection.groups.items() if name == dev["group"] and s != serial and s in by_serial]
        if not case and others:
            try:
                if all(o and Version.parse(o) == pend for o in others):
                    case = "match"
            except VersionError:
                pass
        if not case:
            what = ("its HA peer" if ctx.facts.ha_mode in ("active-passive", "active-active") else "the rest of its group" if dev["group"] else "")
            tail = (f" ({what} is not running {shown}, so this is not a way to get them onto one version)" if what else "")
            _swap(dev, CheckResult("pending_reboot", FAIL, clean_up + tail))
            continue
        window = ((dev["group"] and ctx.settings.group_tolerate_asymmetry)
                  or (not dev["group"] and ctx.facts.ha_mode != "none" and ctx.settings.pair_tolerate_asymmetry))
        dev["activate"] = {"version": str(pend), "case": case}
        words = ("it is the target version, so the upgrade is only the reboot that makes it live" if case == "finish" else
                 "its HA peer or the rest of its group already runs it: the plan reboots into it first so they are on one version, then goes on to the target")
        if window:        # it refuses commits until it has rebooted, and a firewall that is about to reboot has no use for the window anyway
            words += ("; the asymmetry window is opened on this firewall after that reboot (the others take the sessions it drains, and they get the window first)")
        _swap(dev, CheckResult("pending_reboot", WARN, f"{shown} is installed and waiting for a reboot; {words}", ack="pending_reboot"))


def _apply_standalone(devices, ctxs, selection):
    for serial, ctx in ctxs.items():
        dev = devices[serial]
        if dev["verdict"] not in ("ready", "caution"):
            continue
        if dev["group"] and ctx.facts.ha_mode != "none":
            _add(dev, CheckResult("standalone_group", FAIL, f"it is in the stand-alone group {dev['group']!r} but is an HA member "
                                                           f"({ctx.facts.ha_mode}); groups are for firewalls that are not in an HA pair"))
            continue
        if ctx.facts.ha_mode != "none":
            continue
        if dev["group"]:
            _add(dev, CheckResult("standalone_group", INFO, f"member of the stand-alone group {dev['group']!r}: the other members carry the traffic while "
                                                           f"this one reboots (a group reboots {ctx.settings.group_install_tolerance} member(s) at a time)"))
        elif selection.accept_standalone_outage or getattr(ctx.settings, "accept_standalone_outage", False):
            _add(dev, CheckResult("standalone_outage", INFO, "standalone: outage during the reboot accepted by the user"))
        else:
            dev["results"].append(CheckResult("standalone_outage", SKIP,
                                              "standalone firewall: the reboot is an outage; set accept_standalone_outage (Settings) or use --accept-standalone-outage",
                                              kind=EXCLUDED).to_dict())
            dev["verdict"], dev["skip_kind"] = "skipped", EXCLUDED


def _apply_plugins(devices, ctxs, settings):
    """HA members whose plugins differ cannot sync their configuration: PAN-OS refuses it. The unsynced pair is judged here, on the ACTIVE member's word, and both
    members get the same verdict. plugin_mismatch = alert: judged like any pair that cannot be synced (ha_config_unsynced: refused, or flagged at the fail-over), and
    the message says what rebalance would do. rebalance / fullupgrade: held until `prepare` has levelled the plugins, and the message says what it will install."""
    for serial, ctx in ctxs.items():
        f = ctx.facts
        if f is None or f.ha_mode != "active-passive" or f.ha_state != "active" or f.ha_config_sync is not False or devices.get(serial) is None:
            continue
        peer = devices[serial].get("peer_serial")
        peer_facts = ctxs[peer].facts if peer in ctxs else None
        mode = settings.plugin_mismatch
        fixes, names = xmlops.plugin_fixes(f, peer_facts, "fullupgrade" if mode == "fullupgrade" else "rebalance")
        if not names:
            continue
        resolved = bool(fixes)
        names_of = {"a": devices[serial]["hostname"] + " (the ACTIVE member)", "b": devices[peer]["hostname"] if peer in devices else "its peer"}
        will = "; ".join(f"{d['name']}-{d['version']} on {names_of[d['on']]}" for d in fixes)
        for member in [serial] + ([peer] if peer in devices else []):
            dev = devices[member]
            if not any(r["check"] == "ha_peer" and r.get("ack") == "force_config_sync" for r in dev["results"]):
                continue                                           # sync_to_peer = Disable: the ordinary unsynced rules above already judged it
            if mode != "alert" and resolved:
                _swap(dev, CheckResult("ha_peer", FAIL,
                                       f"the passive member's configuration cannot be synchronized with the active member: PAN-OS reports the plugin(s) {', '.join(names)} as "
                                       f"different between the two members, and refuses a sync then ('Version mismatch with Peer'). Run `prepare` first: with plugin_mismatch = "
                                       f"{mode} it installs {will}, then compares what both members report and raises the lower one if they still differ, then syncs. Nothing is removed."))
            elif settings.ha_config_unsynced == "warn":
                text = (f"FAILOVER RISK. The plugin(s) {', '.join(names)} differ between the members, so PAN-OS refuses the configuration sync "
                        f"and this pair's passive member does NOT have the active member's configuration. When the ACTIVE member suspends, the passive runs ITS OWN "
                        f"configuration, so anything that differs changes at that moment. plugin_mismatch is '{mode}'. Accepting this when you prepare the batch lets the upgrade proceed.")
                if member == serial:
                    _swap(dev, CheckResult("ha_peer", WARN, text, ack="failover_to_unsynced_passive"))
                else:
                    _swap(dev, CheckResult("ha_peer", INFO, "config not synchronized with the active member (the plugins differ); the risk is at the later failover "
                                                            "and is flagged on the active member."))
            else:
                _swap(dev, CheckResult("ha_peer", FAIL,
                                       f"the passive member's configuration cannot be synchronized: PAN-OS reports the plugin(s) {', '.join(names)} as different between the members "
                                       f"(it refuses the sync). plugin_mismatch = alert leaves plugins alone"
                                       + (f"; with plugin_mismatch = rebalance `prepare` would install {will}" if will else "; the tool could not tell which version to install") +
                                       ". Review that, then loosen plugin_mismatch, level them yourself, or set ha_config_unsynced = warn to proceed anyway"))


def _apply_pair_rules(devices, ctxs, by_serial, target):
    plannable = {s for s, d in devices.items() if d["verdict"] in ("ready", "caution")}
    for serial in sorted(plannable):
        dev, ctx = devices[serial], ctxs[serial]
        if ctx.facts.ha_mode != "active-passive" or ctx.facts.ha_state not in ("active", "passive", "suspended"):
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
                                  f"blocked_peer to warn to go ahead anyway"))
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
    add("backup", note="running config exported, parsed back and checksummed")
    pos = dev.get("position")                     # "first" / "second" in a pair that is entirely in this run
    active_first = settings.upgrade_first == "active" and bool(pos)
    swaps = role == "active" or (active_first and pos == "second")    # this member is suspended so its peer can take over
    if syncing and (pos == "first" or (pos is None and (role == "passive" or not peer_planned))):
        add("sync to peer", "none", "after this member's own config is backed up, on the ACTIVE member: force its running configuration onto the passive member, then confirm "
                                     "HA1 config sync reads synchronized")
    if pos == "second" or (pos is None and role == "active" and peer_planned):
        peer = devices[dev["peer_serial"]]["hostname"]
        if active_first:
            add(WAIT_FIRST, note=f"this member does not start until {peer} (the member upgraded first) has finished its own upgrade and checks; "
                                 f"its version is confirmed before this member hands over")
        else:
            add(WAIT_PASSIVE, note=f"this member does not start until {peer} (the passive member) has finished its own upgrade and checks; "
                                   f"its version is confirmed before this member hands over")
    if swaps:
        if active_first and pos == "first":
            takes_over = ("its passive peer takes over, which proves that member's cabling and that it carries the traffic: the session count "
                          "must stay roughly stable")
        else:
            takes_over = ("its already upgraded " + ("peer" if active_first else "passive peer") + " takes over") if peer_planned else "its peer takes over"
        add("fail over", "failover",
            f"suspend this member so {takes_over}; wait up to {settings.pair_session_wait_s}s for the new active node's sessions to come within {int(settings.pair_session_warn_band * 100)}% of this one's (warning if still short, "
            f"failure and put this member back if {int(settings.pair_session_fail_band * 100)}% or more short)")
    if hold:
        add("hold suspended", note="preemption is on: keep this member suspended until go-live")
    installs = [p for p in _path_steps(dev) if p["install"]]
    drains = bool(dev.get("group")) and bool(settings.lb_drain_profile)
    traffic = "outage" if role == "standalone" and not dev.get("group") else "redundancy"
    drained = False

    def drain_once():
        nonlocal drained
        if drains and not drained:
            drained = True
            add("drain", "redundancy", f"take {settings.lb_drain_probe_protocol} out of the interface management profile {settings.lb_drain_profile!r} (commit on the "
                                       f"firewall) so the load balancer drains this member, then wait {settings.lb_drain_wait_s}s")
    activate = dev.get("activate")
    if activate:      # no drain first: a firewall with an image waiting for its reboot refuses every commit, and a drain is a commit
        add(f"reboot to activate {activate['version']}", traffic,
            f"{activate['version']} is already installed and waiting for a reboot (it cannot be drained first: it refuses commits until it has rebooted); "
            f"timeout {settings.reboot_timeout_s}s")
        add("verify", note=f"version is {activate['version']}, no jobs running")
    for p in installs:
        drain_once()      # before the install: it gives the load balancer the whole install to drain the member, and once an OLDER image is installed the
                          # firewall refuses every commit until it has rebooted, and a drain is a commit
        add(f"install {p['version']}", note=f"timeout {settings.install_timeout_s}s")
        add("reboot", traffic, f"timeout {settings.reboot_timeout_s}s")
        add("verify", note="version as expected, no jobs running")
    if drained:
        add("undrain", note=f"put the interface management profile {settings.lb_drain_profile!r} back as it was, so the load balancer takes this member back")
    if dev.get("group"):
        add("check service", note=f"wait until this member's session table is back within {int(settings.group_session_warn_band * 100)}% of its count before it "
                                  f"was drained (or of the other members' average), up to {settings.group_session_wait_s}s, before the next member of the group "
                                  f"is touched; a warning if still short then, a failure if {int(settings.group_session_fail_band * 100)}% or more short")
    if hold or swaps:
        add("go live", note="release the suspension so the member can rejoin the pair")
    if role != "standalone":
        add("verify HA", note="HA1 config sync, HA2 session sync, and (on the active node) sessions within "
            f"{int(settings.pair_session_warn_band * 100)}% of the baseline (waited for up to {settings.pair_session_wait_s}s)")
    if role != "standalone" and settings.restore_roles == "restore" and role == "active" and not active_first:
        add("restore roles", "failover", "fail back so the original active member is active again")
    rollback = ("not automatic. The saved config and the previous image remain; a rollback is planned and approved "
                "separately. Exact commands to be verified from vendor documentation before implementation.")
    return steps, rollback


def _order(devices, by_serial, settings=None):
    """Units in plan order: an HA pair is one unit (passive first, or active first when upgrade_first says so), a standalone is one unit."""
    seen, units = set(), []
    for serial, dev in devices.items():
        if dev["verdict"] not in ("ready", "caution") or serial in seen:
            continue
        members = [serial]
        peer = dev["peer_serial"]
        if peer and peer in devices and devices[peer]["verdict"] in ("ready", "caution"):
            members.append(peer)
        placed = len(members) == 2 and all(devices[m].get("position") for m in members)
        members.sort(key=lambda s: (devices[s]["position"] != "first" if placed else devices[s]["ha_state"] not in ("passive", "suspended"), s))
        seen.update(members)
        units.append(members)
    # A stand-alone group member with an image waiting for its reboot goes first: it cannot take the asymmetry window until it has rebooted, so the
    # rest of its group must not start (and hand it their sessions) before it is back.
    units.sort(key=lambda unit: 0 if any(devices[m].get("activate") and devices[m]["group"] for m in unit) else 1)
    return units


def _groups_planned(devices):
    found = {}
    for serial, dev in devices.items():
        if dev["group"] and dev["verdict"] in ("ready", "caution"):
            found.setdefault(dev["group"], []).append(serial)
    return found


def _asymmetry_windows(devices, order, groups, settings, selection=None, by_serial=None):
    """Each place the tool will let a pair or group accept traffic that looks asymmetric: recorded first, restored when the whole pair / group is done."""
    windows = []
    if settings.pair_tolerate_asymmetry:
        for unit in order:
            if devices[unit[0]]["ha_mode"] not in (None, "none") and not devices[unit[0]]["group"]:
                windows.append({"kind": "pair", "name": " / ".join(devices[s]["hostname"] for s in unit), "serials": list(unit)})
    if settings.group_tolerate_asymmetry:
        for name, members in sorted(groups.items()):
            spec = {"kind": "group", "name": name, "serials": list(members)}
            # The sessions a member drops land on the REST of its group, whether or not those firewalls are in this run: they get the window too.
            named = [s for s, other in (selection.groups.items() if selection else []) if other == name and s not in members]
            support = [s for s in named if by_serial and s in by_serial and by_serial[s].connected]
            away = [s for s in named if by_serial and s in by_serial and not by_serial[s].connected]
            if support:
                spec["support"] = support
            if away:
                spec["unreachable"] = away
            windows.append(spec)
    return windows


def _window_words(windows):
    def _how_many(w):
        n, more = len(w["serials"]), len(w.get("support") or [])
        return f"{n} firewall{'s' if n != 1 else ''}" + (f" in this run and {more} more of the same group that are not" if more else "")
    where = "; ".join(f"{w['kind']} {w['name']} ({_how_many(w)})" for w in windows)
    return ("Accepting asymmetric return traffic during the upgrade window changes the live configuration of these firewalls: " + where + ". Before the "
            "first member is touched each firewall's tcp-reject-non-syn and asymmetric-path values are recorded, then tcp-reject-non-syn is set to no and "
            "asymmetric-path to bypass (committed on the firewall); both are put back when the pair or group is finished, whether it succeeded or not. "
            "A firewall that cannot be put back is reported at the end and needs a person.")


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
        "order": plan["order"], "groups": plan["groups"], "asymmetry": plan["asymmetry"],
        "fleet_acks": sorted(r["ack"] for r in plan["fleet_checks"] if r.get("ack")),
    }
    return body
