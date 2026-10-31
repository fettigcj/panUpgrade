# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Preflight checks. Pure functions: context in, CheckResult out, no I/O.

A check that cannot decide returns skip/no_data. A check that crashes becomes an
``error`` result (see run_checks); it never stops the plan and never passes.
"""

from dataclasses import dataclass, field

from . import advisories, paths
from .model import (ERROR, EXCLUDED, FAIL, INFO, NO_DATA, NOT_APPLICABLE, PASS, SKIP, WARN,
                    CheckResult)
from . import releases as releases_mod
from .versions import Version, VersionError


@dataclass
class Context:
    ref: object                 # DeviceRef
    facts: object               # Facts or None
    target: Version
    settings: object
    path_rules: dict
    panorama_version: str = None
    collect_error: str = ""
    panorama_clock: str = None
    extra: dict = field(default_factory=dict)
    releases: object = None         # releases.Releases; the table that ships with the tool when None


def _skip(name, kind, reason):
    return CheckResult(name, SKIP, reason, kind=kind)


def _no_data(name, what):
    return _skip(name, NO_DATA, f"{what} was not collected")


def reachable(ctx):
    if not ctx.ref.connected:
        return _skip("reachable", NO_DATA, "device is not connected to Panorama")
    if ctx.facts is None:
        return _skip("reachable", NO_DATA, f"facts could not be read: {ctx.collect_error or 'unknown reason'}")
    return CheckResult("reachable", PASS, "connected and answering")


def current_version(ctx):
    raw = ctx.facts.sw_version if ctx.facts and ctx.facts.sw_version else ctx.ref.sw_version
    if not raw:
        return _no_data("current_version", "the running version")
    try:
        version = Version.parse(raw)
    except VersionError as exc:
        return _skip("current_version", NO_DATA, str(exc))
    if version == ctx.target:
        return _skip("current_version", NOT_APPLICABLE, f"already running {version}, the target")
    if version > ctx.target:
        if not ctx.settings.allow_downgrade:
            return _skip("current_version", NOT_APPLICABLE,
                         f"already running {version}, newer than the target {ctx.target} (set allow_downgrade to install down to it)")
        if version.feature_key != ctx.target.feature_key:
            if not ctx.settings.allow_cross_feature_downgrade:
                return _skip("current_version", NOT_APPLICABLE,
                             f"running {version}, a different feature release from the target {ctx.target}; "
                             f"set allow_cross_feature_downgrade to go down across feature releases")
            return CheckResult("current_version", WARN,
                               f"DOWNGRADE ACROSS FEATURE RELEASES: running {version}, target {ctx.target}. The configuration written by "
                               f"the newer release may not load on the older one; have a saved configuration of the older release ready.",
                               ack="downgrade_cross_feature")
        return CheckResult("current_version", WARN, f"DOWNGRADE: running {version}, newer than the target {ctx.target}",
                           ack="downgrade")
    return CheckResult("current_version", PASS, f"running {version}, older than the target {ctx.target}")


def upgrade_path(ctx):
    raw = ctx.facts.sw_version if ctx.facts and ctx.facts.sw_version else ctx.ref.sw_version
    try:
        result = paths.compute(raw, str(ctx.target), ctx.path_rules, ctx.settings.allow_downgrade,
                               ctx.settings.allow_cross_feature_downgrade)
    except VersionError as exc:
        return _skip("upgrade_path", NO_DATA, str(exc))
    if not result.ok:
        if result.kind == NOT_APPLICABLE:
            return _skip("upgrade_path", NOT_APPLICABLE, result.reason)
        return _skip("upgrade_path", NO_DATA, result.reason)
    evidence = {"steps": result.steps}
    if not result.verified:
        return CheckResult("upgrade_path", WARN, f"{result.reason}; the path rules are marked unverified",
                           ack="path_rules_unverified", evidence=evidence)
    return CheckResult("upgrade_path", PASS, result.reason, evidence=evidence)


def content_minimum(ctx):
    """PAN-OS refuses to install some releases unless the dynamic content is at least a given version. Checked up front so the install does not fail mid-run."""
    raw = ctx.facts.sw_version if ctx.facts and ctx.facts.sw_version else ctx.ref.sw_version
    try:
        result = paths.compute(raw, str(ctx.target), ctx.path_rules, ctx.settings.allow_downgrade, ctx.settings.allow_cross_feature_downgrade)
    except VersionError as exc:
        return _skip("content_minimum", NO_DATA, str(exc))
    if not result.ok:
        return _skip("content_minimum", NOT_APPLICABLE, result.reason)
    need, version = paths.content_requirement(ctx.path_rules, result.steps)
    if need is None:
        return _skip("content_minimum", NOT_APPLICABLE, "no minimum-content rule is on file for the releases this path installs")
    have_text = ((ctx.facts.content_versions or {}).get("app") if ctx.facts else None)
    try:
        have = int(have_text.split("-")[0])
    except (AttributeError, ValueError):
        have = 0
    if have < need:
        return CheckResult("content_minimum", FAIL,
                           f"installing {version} needs dynamic content {need} or newer; this firewall has {have_text or 'none loaded'}. PAN-OS will "
                           f"refuse the install. `panupgrade prepare --target {ctx.target}` installs the latest content first.",
                           evidence={"needed": need, "have": have_text})
    return CheckResult("content_minimum", PASS, f"content {have_text} meets the {need} that {version} needs")


def ha_state(ctx):
    f = ctx.facts
    if f.ha_mode is None:
        return _no_data("ha_state", "HA mode")
    if f.ha_mode == "none":
        return CheckResult("ha_state", INFO, "standalone: the upgrade reboot is an outage")
    if f.ha_mode not in ("active-passive", "active-active"):
        return CheckResult("ha_state", FAIL, f"unrecognised HA mode {f.ha_mode!r}")
    if f.ha_state is None:
        return _no_data("ha_state", "HA state")
    if f.ha_state not in ("active", "passive", "active-primary", "active-secondary"):
        return CheckResult("ha_state", FAIL, f"HA state is {f.ha_state!r}; expected a healthy active or passive member")
    return CheckResult("ha_state", PASS, f"{f.ha_mode}, this member is {f.ha_state}")


def ha_peer(ctx):
    f = ctx.facts
    if f.ha_mode in (None, "none"):
        return _skip("ha_peer", NOT_APPLICABLE, "not in an HA pair")
    problems, unknown = [], []
    for label, value in (("HA2 session sync", f.ha_session_sync), ("HA1 config sync", f.ha_config_sync),
                         ("HA links", f.ha_links_ok)):
        if value is None:
            unknown.append(label)
        elif value is False:
            problems.append(label)
    if problems == ["HA1 config sync"] and f.ha_session_sync and f.ha_links_ok is not False:
        if ctx.settings.sync_to_peer != "Disable":
            return CheckResult("ha_peer", WARN,
                               "the passive member's configuration is not synchronized with the active member; the upgrade run "
                               "will force a sync (active to passive) before touching the passive member",
                               ack="force_config_sync")
        if ctx.settings.ha_config_unsynced == "caution":
            marker = {"config_unsynced": True}
            if f.ha_state == "active":
                return CheckResult(
                    "ha_peer", WARN,
                    "FAILOVER RISK. This pair's passive member does not have this member's configuration (HA config sync reads 'not synchronized'). "
                    "When this ACTIVE member suspends so the upgraded passive can take over, the passive runs ITS OWN configuration, so anything that "
                    "differs (rules, routes, objects, VPNs, anything) changes at that moment: traffic can be handled differently, dropped or "
                    "allowed. The upgrade itself does not fix that. Sync the configuration first if you can (sync_to_peer = Both does it). "
                    "Accepting this when you prepare the batch lets the upgrade proceed.", ack="failover_to_unsynced_passive", evidence=marker)
            return CheckResult("ha_peer", INFO,
                               "config not synchronized with the active member. That does not affect this passive member's own upgrade (it carries no "
                               "traffic); the risk is at the later failover and is flagged on the active member.", evidence=marker)
        return CheckResult("ha_peer", FAIL, "not healthy: HA1 config sync is not synchronized and sync_to_peer is Disable. Sync it yourself, "
                                            "set sync_to_peer to Both so the tool forces the sync, or set ha_config_unsynced=caution to "
                                            "proceed anyway")
    if problems:
        return CheckResult("ha_peer", FAIL, "not healthy: " + ", ".join(problems))
    if unknown:
        return _skip("ha_peer", NO_DATA, "not collected: " + ", ".join(unknown))
    return CheckResult("ha_peer", PASS, "session sync (HA2), config sync (HA1) and links are healthy")


def preemption(ctx):
    f = ctx.facts
    if f.ha_mode in (None, "none"):
        return _skip("preemption", NOT_APPLICABLE, "not in an HA pair")
    if f.preemption is None:
        return _no_data("preemption", "the preemption setting")
    if f.preemption:
        return CheckResult("preemption", INFO,
                           "preemption is on: the member being upgraded is kept suspended until go-live "
                           "so it cannot take traffic early")
    return CheckResult("preemption", PASS, "preemption is off")


def disk_space(ctx):
    free = ctx.facts.disk_free_mb
    if free is None:
        return _no_data("disk_space", "free disk space")
    need = ctx.settings.min_free_disk_mb
    if free < need:
        return CheckResult("disk_space", FAIL, f"{free} MB free, setting min_free_disk_mb is {need}")
    return CheckResult("disk_space", PASS, f"{free} MB free (needs {need})")


def pending_changes(ctx):
    pending = ctx.facts.pending_changes
    mode = ctx.settings.pending_changes
    if pending is None:
        return _no_data("pending_changes", "the pending-changes state")
    if not pending:
        return CheckResult("pending_changes", PASS, "no uncommitted candidate config")
    msg = "uncommitted candidate config exists and is not part of the running config"
    if mode == "block":
        return CheckResult("pending_changes", FAIL, msg + " (setting pending_changes=block)")
    if mode == "warn":
        return CheckResult("pending_changes", WARN, msg + " (setting pending_changes=warn)", ack="pending_changes")
    return CheckResult("pending_changes", INFO, msg + " (setting pending_changes=ignore)")


def running_jobs(ctx):
    jobs = ctx.facts.running_jobs
    if jobs is None:
        return _no_data("running_jobs", "the job list")
    if jobs:
        return CheckResult("running_jobs", WARN, f"{jobs} job(s) still running", ack="running_jobs")
    return CheckResult("running_jobs", PASS, "no jobs running")


def session_baseline(ctx):
    """The active node's session count is the yardstick for the failover and post-upgrade bands."""
    f = ctx.facts
    if f.ha_mode == "active-passive" and f.ha_state == "passive":
        return _skip("session_baseline", NOT_APPLICABLE, "passive node carries no traffic; the active node is judged")
    count = f.session_count
    if count is None:
        return _no_data("session_baseline", "the session count")
    if count == 0:
        return CheckResult("session_baseline", WARN, "0 sessions: a before/after comparison cannot detect traffic loss",
                           ack="no_session_baseline")
    return CheckResult("session_baseline", PASS, f"{count} active sessions recorded as the baseline")


def certificate_advisory(ctx):
    """Is this build (and the target) one that carries the fix for the certificate-expiry advisories? Advice for the running
    build, a caution when the TARGET is itself an affected build (upgrading to it would not cure the problem)."""
    raw = ctx.facts.sw_version if ctx.facts and ctx.facts.sw_version else ctx.ref.sw_version
    if not raw:
        return _no_data("certificate_advisory", "the running version")
    try:
        running = Version.parse(raw)
    except VersionError as exc:
        return _skip("certificate_advisory", NO_DATA, str(exc))
    content = ((ctx.facts.content_versions or {}).get("app") if ctx.facts else None)
    data = advisories.load()
    verdict = advisories.assess(data["advisories"], running, content, ctx.target)
    if verdict["target_affected"]:
        return CheckResult("certificate_advisory", WARN,
                           f"the target {ctx.target} is itself a build listed as affected by: {'; '.join(verdict['target_affected'])}. "
                           f"Upgrading to it would not cure the expired certificates; choose a build that contains the fix "
                           f"(advisory tables are unverified, see cert_advisories.json).",
                           ack="target_cert_advisory", evidence={"target_affected": verdict["target_affected"]})
    problems = []
    for adv, state in verdict["now"]:
        if state == "affected":
            problems.append(f"running {running} with content {content or 'unknown'} is affected by: {adv['title']} (certificates may already be "
                            f"expired, which shows up as Panorama/HA/cloud trouble; the upgrade is the cure)")
        elif state == "needs_restart":
            problems.append(f"the content installed fixes: {adv['title']}, but the fix only takes effect after a restart (the upgrade's reboot does it)")
    if problems:
        return CheckResult("certificate_advisory", WARN, "; ".join(problems), evidence={"states": [s for _, s in verdict["now"]]})
    return CheckResult("certificate_advisory", PASS, f"{running} carries the fixes for the certificate advisories")


def device_certificate(ctx):
    """The device certificate (`show device-certificate status`). Needed by cloud services and, on some builds, by Panorama links."""
    cert = ctx.facts.device_cert
    if cert is None:
        return _no_data("device_certificate", "the device certificate status")
    if cert.get("status") == "success" and cert.get("validity") == "Valid":
        return CheckResult("device_certificate", PASS, f"valid until {cert.get('not_valid_after')}")
    if cert.get("validity") == "Not yet valid":
        return CheckResult("device_certificate", WARN,
                           f"the device certificate starts {cert.get('not_valid_before')}, which this firewall's clock has not reached yet "
                           f"('Not yet valid'). This is almost always a clock that runs behind (see clock_skew); until it is right, "
                           f"certificate-based links can fail.", evidence=cert)
    return CheckResult("device_certificate", WARN,
                       f"no valid device certificate ({cert.get('validity') or cert.get('status')}: {cert.get('msg') or 'no detail'}); "
                       f"cloud services, and on some builds Panorama and HA exchanges, can fail", evidence=cert)


def clock_skew(ctx):
    """The firewall's clock against Panorama's. A firewall hours off sees valid certificates as not yet valid or expired."""
    if not ctx.facts.clock_utc or not ctx.panorama_clock:
        return _no_data("clock_skew", "a clock reading (this firewall or Panorama)")
    from datetime import datetime
    skew = (datetime.fromisoformat(ctx.facts.clock_utc) - datetime.fromisoformat(ctx.panorama_clock)).total_seconds()
    if abs(skew) > 300:
        return CheckResult("clock_skew", WARN,
                           f"this firewall's clock is {abs(skew) / 3600:.1f} h {'behind' if skew < 0 else 'ahead of'} Panorama's. Certificates then read "
                           f"as not yet valid or expired. Set NTP (or fix the hypervisor's time source) before relying on certificate-based links.",
                           evidence={"skew_seconds": round(skew)})
    return CheckResult("clock_skew", PASS, f"within {int(abs(skew))} s of Panorama")


def ntp(ctx):
    if ctx.facts.ntp_synced is None:
        return _no_data("ntp", "NTP status")
    return CheckResult("ntp", PASS if ctx.facts.ntp_synced else WARN,
                       "time is synchronised" if ctx.facts.ntp_synced else "time is not synchronised",
                       ack="" if ctx.facts.ntp_synced else "ntp")


def licenses(ctx):
    if ctx.facts.licenses_ok is None:
        return _no_data("licenses", "licence status")
    return CheckResult("licenses", PASS if ctx.facts.licenses_ok else WARN,
                       "licences are valid" if ctx.facts.licenses_ok else "a licence is expired or missing",
                       ack="" if ctx.facts.licenses_ok else "licenses")


def panorama_range(ctx):
    """Advice only, never a blocker: a firewall far behind Panorama is what this tool exists to fix."""
    if not ctx.panorama_version:
        return _no_data("panorama_range", "Panorama's version")
    raw = ctx.facts.sw_version if ctx.facts and ctx.facts.sw_version else ctx.ref.sw_version
    if not raw:
        return _no_data("panorama_range", "the running version")
    pano, current = Version.parse(ctx.panorama_version), Version.parse(raw)
    table, unit, limit = ctx.releases or releases_mod.default(), ctx.settings.panorama_compare, ctx.settings.spread_limit
    behind = table.behind(pano, current, unit)
    names = _unit_names(unit)
    stale = _stale_note(table, pano, current, ctx.target)
    if behind > limit:
        after = table.behind(pano, ctx.target, unit)
        tail = ("" if after <= limit else
                f" The target {ctx.target} would still be {after} {names} behind Panorama; consider a newer target.")
        return CheckResult("panorama_range", WARN,
                           f"advise upgrade: running {current}, {behind} {names} behind Panorama {pano} "
                           f"(limit {limit}). Panorama converts its configuration to each firewall's version, and that is "
                           f"least trustworthy far back. This does not hold the upgrade; catching up is the fix.{tail}{stale}",
                           evidence={"behind": behind, "limit": limit, "unit": unit})
    return CheckResult("panorama_range", PASS, f"{current} is within {limit} {names} of Panorama {pano}{stale}")


# (function, required). A required check with no data blocks the device: a
# collection problem must never look like success.
DEVICE_CHECKS = [
    (reachable, True), (current_version, True), (upgrade_path, True), (ha_state, True),
    (ha_peer, True), (preemption, False), (disk_space, True), (pending_changes, True),
    (running_jobs, False), (session_baseline, False), (ntp, False), (licenses, False), (panorama_range, False), (certificate_advisory, False), (device_certificate, False), (clock_skew, False), (content_minimum, False),
]
REQUIRED = {fn.__name__ for fn, req in DEVICE_CHECKS if req}


def run_device_checks(ctx):
    """Run every check in order. Stops early only when the device cannot be read at all."""
    results = []
    first = _guard(reachable, ctx)
    results.append(first)
    if first.status != PASS:
        return results
    for fn, _required in DEVICE_CHECKS[1:]:
        results.append(_guard(fn, ctx))
    return results


def _guard(fn, ctx):
    try:
        return fn(ctx)
    except Exception as exc:  # a broken check is reported, never fatal, never a pass
        return CheckResult(fn.__name__, ERROR, f"check crashed: {type(exc).__name__}: {exc}")


# ---- fleet-wide checks (Panorama vs firewalls) ----

def _unit_names(unit):
    return "major versions" if unit == "major" else "minor versions (feature releases)"


def _stale_note(table, *versions):
    """A sentence to add when a release is missing from the table, so a stale table is noticed rather than trusted."""
    missing = table.unlisted(*versions)
    return (f" (The release table does not list {', '.join(missing)}, so {'it is' if len(missing) == 1 else 'they are'} counted in numeric "
            f"order; `panupgrade releases --update` refreshes the table.)") if missing else ""


def fleet_checks(panorama_version, target, planned_versions, settings, table=None):
    """planned_versions: versions the selected firewalls will have after the plan, plus the unselected as they are."""
    out = []
    if not panorama_version:
        return [_skip("panorama_version", NO_DATA, "Panorama version was not collected")]
    try:
        pano = Version.parse(panorama_version)
    except VersionError as exc:
        return [_skip("panorama_version", NO_DATA, str(exc))]
    table = table or releases_mod.default()
    older = pano.major < target.major if settings.panorama_compare == "major" else pano.feature_key < target.feature_key
    if older:
        out.append(CheckResult(
            "panorama_not_older", WARN,
            f"Panorama runs {pano}, older than the target {target}. Panorama should be at least as new as the "
            f"firewalls it manages; upgrading Panorama is outside this tool.",
            ack="panorama_older_than_target"))
    else:
        out.append(CheckResult("panorama_not_older", PASS, f"Panorama {pano} is not older than the target {target}"))
    if planned_versions:
        limit, unit = settings.spread_limit, settings.panorama_compare
        names, stale = _unit_names(unit), _stale_note(table, pano, *planned_versions)
        behind = sorted(v for v in planned_versions if table.behind(pano, v, unit) > limit)
        if behind:
            out.append(CheckResult(
                "version_spread", WARN,
                f"advise upgrade: after this plan {len(behind)} of {len(planned_versions)} firewall(s) are still more than "
                f"{limit} {names} behind Panorama {pano} (oldest: {behind[0]}). Not a blocker: this tool upgrades "
                f"firewalls, never Panorama; catching up is the fix. Panorama must not be upgraded further while these lag.{stale}",
                evidence={"behind": len(behind), "limit": limit, "unit": unit}))
        else:
            out.append(CheckResult("version_spread", PASS,
                                   f"after this plan every firewall is within {limit} {names} of Panorama {pano}{stale}"))
    return out
