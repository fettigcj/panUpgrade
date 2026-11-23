# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""User-tunable settings: one plain TOML file, strict types, safe defaults.

Secrets never appear here: a key that looks like one (password, token, api key, ...) is rejected.
"""

import re
import tomllib
from dataclasses import dataclass, fields

_SECRET_WORDS = ("password", "passwd", "secret", "token", "apikey", "api_key")


# What a load balancer probes a firewall with. Each name is the protocols of the interface management profile that the probe needs; "+" joins them.
PROBE_CHOICES = ("ssh", "http", "https", "ssh+http", "ssh+https", "http+https", "ssh+http+https", "ping")


class SettingsError(ValueError):
    pass


@dataclass
class Settings:
    # HA pairs: the session table is judged where it is carried. Only the ACTIVE member sees sessions built on it; the passive member holds a copy over
    # the HA2 link, so the count the pair had before (the active member's) is what the member that takes over must show. Judged after a fail-over
    # (does the new active member hold the table?) and when the last member is done (is the service where it was?). The member is waited for up to
    # pair_session_wait_s until it is no more than pair_session_warn_band short; then still less than pair_session_fail_band short is a WARNING and
    # that short or more is a FAILURE (after a fail-over: the suspended member is put back into service first). Shortfall only.
    # A session count below this is too small to judge: a table of 12 sessions of management traffic moves by more than a band for no reason at all.
    # Below it the session checks are skipped (and say so); the plan and prepare say it beforehand. 0 sessions is always below it.
    session_noise_floor: int = 100
    pair_session_warn_band: float = 0.35
    pair_session_fail_band: float = 0.80
    pair_session_wait_s: int = 60
    # Panorama should stay within this many versions of the firewalls it manages (a rule of thumb from experience with schema
    # conversion), counted in the unit panorama_compare names. A firewall further behind gets "advise upgrade"; it never blocks that
    # firewall's upgrade.
    spread_limit: int = 2
    # The unit versions are compared in, for "Panorama is older than the target" and for spread_limit: minor (feature releases,
    # 12.2 > 12.1 > 11.2, counted from the table of releases PAN-OS really has) | major (12 > 11 > 10).
    panorama_compare: str = "minor"
    # Batch handling
    max_failures: int = 5
    max_parallel: int = 5               # units (an HA pair, or one standalone) worked on at the same time; a unit's members stay in series
    api_rate_per_s: float = 5.0         # polite ceiling on requests per second to Panorama, shared by every worker
    # Active-to-peer config sync ("sync to peer"): Disable (never force it; only report) | Both (the separate `prepare` step does it
    # ahead of the window AND the upgrade run does it again first, for each pair that needs it) | UpgradeOnly (only the upgrade run).
    # Production actives should always be syncing to the passive; the sync forces the passive to match what is running.
    sync_to_peer: str = "Both"
    sync_timeout_s: int = 600
    settle_after_failover_s: int = 60   # after suspending the active member: wait this long for the commit it queues itself, then until it is done (0 = no window)
    settle_after_reboot_s: int = 120    # after a reboot: the same, for the second auto-commit that follows the reconnect to Panorama
    busy_wait_s: int = 900             # how long to keep waiting, and asking again, when a firewall is busy with a commit or another job
    # A pair whose passive member's config is NOT synchronized. The risk is exactly one moment: the ACTIVE member suspends and the
    # upgraded passive takes over running its own, different configuration. block (the default): refuse to plan such a pair at all,
    # so nobody runs into it unread. warn: that moment is flagged on the active member with a plain-words message, to be accepted
    # when the batch is prepared; the passive's own upgrade is unaffected. Only used when sync_to_peer = Disable: with Both or
    # UpgradeOnly the tool syncs the pair itself instead.
    ha_config_unsynced: str = "block"
    # HA members whose INSTALLED plugins differ: PAN-OS refuses the config sync between them ("Version mismatch with Peer for OC"). fix (the default):
    # `prepare` installs on the member that lacks it the plugin the other member runs (a plugin only one member has; at different versions the higher
    # one), through the firewall's own update server, then syncs. Nothing is ever removed or taken down. leave: plugins are not touched, the pair
    # cannot be synced, and what happens to it is decided as for any pair that cannot be synced (ha_config_unsynced).
    plugin_mismatch: str = "fix"
    # A pair whose HA peer is BLOCKED by a check (or could not be read at all). The active member's upgrade hands traffic to the peer
    # for the whole reboot, and either member's upgrade leaves the pair on two releases, so the tool does not go ahead with half of a
    # pair whose other half it has just found a problem with. block (the default): the member that could be upgraded is held too, with
    # the peer's reason. warn: it is planned anyway and only flagged. A peer that is merely not selected, or already on the target,
    # is not a problem and is only flagged either way.
    blocked_peer: str = "block"
    # Dynamic content (the cloud-delivered security services: applications/threats, antivirus, WildFire). HA peers should run
    # the same versions, and a mismatch gates the config sync. never | prepare (install the latest on both members as part of the
    # approved preparation; the approval prompt lists it and says it is outside the customer's own content schedule).
    content_settle_s: int = 90      # after "check now" on both members, how long to wait for PAN-OS's own HA verdict to turn to Match before installing
    sync_content: str = "prepare"
    # Installing an older release than the one running. Each one needs an acknowledgement; going down across a feature release
    # also says that the newer release's configuration may not load on the older one.
    allow_downgrade: bool = False
    # What to do about a pending (uncommitted) candidate config: block | warn | ignore
    pending_changes: str = "block"
    # An HA member that is suspended when planned carries no traffic, so it is upgraded like a passive member (first in its pair). The tool never
    # suspends it again or re-enables it as a decision of its own. false: a suspended member is blocked (make it functional first).
    upgrade_suspended: bool = True
    # Allow Asymmetric Return During Upgrade Window. When an active member is suspended or rebooted, the half-open TCP handshakes (and, with no HA2
    # link, every session) it held are not on the member that takes over, so the survivor sees the rest of those flows as asymmetric and drops them.
    # With this on, before the first member of the pair (or group) is touched the tool records each member's current values, sets
    # `set session tcp-reject-non-syn no` (operational, lost at reboot) and `deviceconfig setting tcp asymmetric-path bypass` (config, committed on
    # the firewall), and puts both back when the pair (or group) is finished, whether it succeeded or not. Every change and every restore is journaled
    # and an unrestored firewall is reported loudly at the end. Pairs: optional (HA2 keeps ordinary sessions). Stand-alone groups: nearly mandatory.
    pair_tolerate_asymmetry: bool = False
    group_tolerate_asymmetry: bool = True
    # Stand-alone groups (firewalls upgraded together that are not HA pairs, e.g. behind a cloud load balancer): how many members of one group may be
    # rebooting / out of service at the same time. 1 = one at a time.
    group_install_tolerance: int = 1
    # Stand-alone firewalls that are not in a group go offline while they reboot. Off: they are left out of the plan (and the plan says so);
    # on: they are planned, and the plan says each one will be offline.
    accept_standalone_outage: bool = False
    # Draining a stand-alone group member from a load balancer. The balancer (an Azure load balancer, say) probes a dataplane interface of each firewall
    # with SSH or HTTP, which the interface management profile on that interface permits. lb_drain_profile names that profile (empty = never drain).
    # Before a member reboots the tool takes the listed protocols out of THAT profile (a commit on the firewall), waits lb_drain_wait_s for the balancer
    # to see the probe fail and drain the member gracefully, upgrades it, and puts the profile back, whether the upgrade worked or not.
    lb_drain_profile: str = ""
    lb_drain_probe_protocol: str = "ssh"      # what the balancer probes with: one of PROBE_CHOICES, e.g. ssh, http, ssh+http
    lb_drain_wait_s: int = 60
    # After a group member is back (upgraded, undrained) the tool waits for its session table to recover before the next member is drained. The member
    # is judged against its own count from before it was drained, or the average of the other members now, whichever it is closer to. Back within
    # group_session_warn_band of that: go on. Still further short when group_session_wait_s runs out: a WARNING if less than group_session_fail_band
    # short, a FAILURE (the group is not started further) at or beyond it. 0.35 / 0.80 / 600.
    group_session_warn_band: float = 0.35
    group_session_fail_band: float = 0.80
    group_session_wait_s: int = 600
    # HA roles after both members are upgraded: leave | restore
    restore_roles: str = "leave"
    # Which member of an active/passive pair is upgraded first: passive (the default: it carries no traffic, so nothing is at risk until the one
    # fail-over) | active (suspend the active member first, prove the passive one carries the traffic, upgrade the suspended member, then fail over
    # again for the other one: two fail-overs per pair, and each one is checked against the session count).
    upgrade_first: str = "passive"
    # Who fetches images: firewall (default, downloads from the vendor) | panorama
    image_source: str = "firewall"
    # `prepare` deletes the PAN-OS images a firewall does not need for this upgrade (it keeps the running image, the base images
    # underneath the running and target releases, every image on the upgrade path, and the target). Off: nothing is ever deleted.
    delete_unneeded_images: bool = False
    # Placeholders, conservative; tune them from your own first-batch evidence
    min_free_disk_mb: int = 4096
    download_timeout_s: int = 1800
    install_timeout_s: int = 1800
    job_stall_s: int = 300             # a download whose status and percentage have not moved for this long is given up on (0 = never; a queued job is exempt)
    reboot_timeout_s: int = 1920
    poll_interval_s: int = 15          # how often a long job or a rebooting firewall is checked
    backup_dir: str = "backups"        # where verified config backups are written
    backup_keep_days: int = 14         # backups older than this are offered for deletion once, when the tool is started (0 = never ask)
    approval_max_age_hours: int = 168  # an approval older than this (a week) is refused (0 = never expires)
    unattended: str = "continue"       # a real run goes on when nobody is looking at it (continue) or starts nothing new until someone looks again (pause)
    unattended_pause_after_min: int = 15   # with unattended = pause: how long without a viewer before the run pauses

    def loosening_warnings(self):
        """Plain-language cautions for settings that make a run riskier. Never an error."""
        out = []
        if self.pending_changes == "revert":
            out.append("pending_changes=revert: uncommitted candidate configuration on a firewall the run touches will be DISCARDED (reverted to the running configuration).")
        elif self.pending_changes != "block":
            out.append(f"pending_changes={self.pending_changes}: uncommitted candidate config will not stop an upgrade.")
        if self.pair_session_warn_band > 0.6 or self.pair_session_fail_band > 0.95 or self.group_session_warn_band > 0.6 or self.group_session_fail_band > 0.95:
            out.append("Session-count bands are very wide; a real traffic loss could pass unnoticed.")
        if self.ha_config_unsynced == "warn":
            out.append("ha_config_unsynced=warn: a pair whose passive config is not synchronized is planned anyway; when the active member "
                       "suspends, the passive takes over running its own, different configuration.")
        if self.blocked_peer == "warn":
            out.append("blocked_peer=warn: a member whose HA peer is blocked by a check is planned anyway; the active member hands traffic to "
                       "that peer while it reboots.")
        if self.pair_tolerate_asymmetry:
            out.append("pair_tolerate_asymmetry=true: during an HA pair's window both members are set to accept traffic that looks asymmetric "
                       "(tcp-reject-non-syn off, asymmetric-path bypass, committed on the firewall) and put back afterwards.")
        if self.allow_downgrade:
            out.append("allow_downgrade=true: firewalls running a newer release than the target are installed down to it (across feature "
                       "releases too; the newer release's configuration may not load on the older one).")
        if self.delete_unneeded_images:
            out.append("delete_unneeded_images=true: `prepare` deletes the PAN-OS images on a firewall that this upgrade does not need "
                       "(it keeps the running image, the base images and the target).")
        return out


_CHOICES = {
    "pending_changes": ("block", "warn", "ignore", "revert"),
    "restore_roles": ("leave", "restore"),
    "upgrade_first": ("passive", "active"),
    "image_source": ("firewall", "panorama"),
    "panorama_compare": ("minor", "major"),
    "sync_to_peer": ("Disable", "Both", "UpgradeOnly"),
    "sync_content": ("never", "prepare"),
    "unattended": ("continue", "pause"),
    "ha_config_unsynced": ("block", "warn"),
    "plugin_mismatch": ("fix", "leave"),
    "blocked_peer": ("block", "warn"),
    "lb_drain_probe_protocol": PROBE_CHOICES,
}


def _coerce(name, value, typ):
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{name} must be a number, got {value!r}")
        return float(value)
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{name} must be a whole number, got {value!r}")
        return value
    if typ is bool:
        if not isinstance(value, bool):
            raise SettingsError(f"{name} must be true or false, got {value!r}")
        return value
    if typ is str:
        if not isinstance(value, str):
            raise SettingsError(f"{name} must be text, got {value!r}")
        return value
    raise SettingsError(f"unsupported setting type for {name}")


RETIRED = {"failover_band": "pair_session_warn_band, pair_session_fail_band and pair_session_wait_s",
           "post_upgrade_band": "pair_session_warn_band, pair_session_fail_band and pair_session_wait_s"}


def from_dict(data, ignored=None, forgiven=()):
    """``forgiven``: names that came out of a settings file. One of those that is not a setting (a typo, a hand edit, a flag from another version)
    is left out and its name added to ``ignored``, never an error; a name typed on the command line that is not a setting still is one."""
    known = {f.name: f.type for f in fields(Settings)}
    kwargs = {}
    for name, value in data.items():
        lowered = name.lower()
        if any(word in lowered for word in _SECRET_WORDS):
            raise SettingsError(f"{name}: secrets do not belong in settings; sign-in is set with --creds-file / --account (see `panupgrade help`)")
        if name in RETIRED:
            raise SettingsError(f"{name} was replaced by {RETIRED[name]}: a warning band, a failure band and how long to wait (see `panupgrade settings`)")
        if name not in known:
            if name in forgiven and ignored is not None:
                ignored.append(name)
                continue
            raise SettingsError(f"unknown setting {name!r}")
        kwargs[name] = _coerce(name, value, known[name])
    settings = Settings(**kwargs)
    for name, choices in _CHOICES.items():
        by_lower = {c.lower(): c for c in choices}
        value = getattr(settings, name)
        if value.lower() == "caution" and "warn" in by_lower:
            value = "warn"                                             # the old name of warn, still read from older settings files
        if value.lower() not in by_lower:
            raise SettingsError(f"{name} must be one of {', '.join(choices)}")
        setattr(settings, name, by_lower[value.lower()])               # 'both' and 'Both' are the same choice
    if settings.api_rate_per_s <= 0:
        raise SettingsError("api_rate_per_s must be above 0")
    for name in ("pair_session_warn_band", "pair_session_fail_band", "group_session_warn_band", "group_session_fail_band"):
        if not 0 < getattr(settings, name) <= 1:
            raise SettingsError(f"{name} must be between 0 and 1")
    for name in ("content_settle_s", "settle_after_failover_s", "settle_after_reboot_s"):
        if getattr(settings, name) < 0:
            raise SettingsError(f"{name} must not be negative")
    if settings.lb_drain_profile and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,30}", settings.lb_drain_profile):
        raise SettingsError("lb_drain_profile must be the name of an interface management profile (letters, digits, space, . _ -, at most 31)")
    for kind in ("pair", "group"):
        if getattr(settings, f"{kind}_session_warn_band") > getattr(settings, f"{kind}_session_fail_band"):
            raise SettingsError(f"{kind}_session_warn_band must not be above {kind}_session_fail_band")
    for name in ("lb_drain_wait_s", "group_session_wait_s", "pair_session_wait_s", "session_noise_floor", "job_stall_s", "backup_keep_days"):
        if getattr(settings, name) < 0:
            raise SettingsError(f"{name} must not be negative")
    for name in ("group_install_tolerance", "spread_limit", "max_failures", "max_parallel", "sync_timeout_s", "busy_wait_s", "poll_interval_s",
                 "unattended_pause_after_min"):
        if getattr(settings, name) < 1:
            raise SettingsError(f"{name} must be at least 1")
    return settings


def load(path=None, overrides=None, ignored=None):
    """``ignored``: a list that gets the name of every unrecognized flag in the file (which are skipped, not refused); see ``from_dict``."""
    data = {}
    if path:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    in_file = set(data)
    for item in overrides or []:
        if "=" not in item:
            raise SettingsError(f"--set expects name=value, got {item!r}")
        name, raw = item.split("=", 1)
        data[name.strip()] = _parse_override(name.strip(), raw.strip())
    return from_dict(data, ignored if ignored is not None else [], in_file - {item.split('=', 1)[0].strip() for item in overrides or [] if '=' in item})


def _parse_override(name, raw):
    kind = {f.name: f.type for f in fields(Settings)}.get(name)
    if kind is bool:
        if raw.lower() not in ("true", "false"):
            raise SettingsError(f"{name} must be true or false, got {raw!r}")
        return raw.lower() == "true"
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            raise SettingsError(f"{name} must be a whole number, got {raw!r}") from None
    if kind is float:
        try:
            return float(raw)
        except ValueError:
            raise SettingsError(f"{name} must be a number, got {raw!r}") from None
    return raw
