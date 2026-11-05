# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""User-tunable settings: one plain TOML file, strict types, safe defaults.

Secrets never appear here: a key that looks like one (password, token, api key, ...) is rejected.
"""

import tomllib
from dataclasses import dataclass, fields

_SECRET_WORDS = ("password", "passwd", "secret", "token", "apikey", "api_key")


class SettingsError(ValueError):
    pass


@dataclass
class Settings:
    # HA / session checks (fractions of the active node's session count)
    failover_band: float = 0.30
    post_upgrade_band: float = 0.50
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
    # HA roles after both members are upgraded: leave | restore
    restore_roles: str = "leave"
    # Who fetches images: firewall (default, downloads from the vendor) | panorama
    image_source: str = "firewall"
    # `prepare` deletes the PAN-OS images a firewall does not need for this upgrade (it keeps the running image, the base images
    # underneath the running and target releases, every image on the upgrade path, and the target). Off: nothing is ever deleted.
    delete_unneeded_images: bool = False
    # Placeholders, conservative; tune them from your own first-batch evidence
    min_free_disk_mb: int = 4096
    download_timeout_s: int = 3600
    install_timeout_s: int = 3600
    reboot_timeout_s: int = 1920
    poll_interval_s: int = 15          # how often a long job or a rebooting firewall is checked
    backup_dir: str = "backups"        # where verified config backups are written
    approval_max_age_hours: int = 168  # an approval older than this (a week) is refused (0 = never expires)

    def loosening_warnings(self):
        """Plain-language cautions for settings that make a run riskier. Never an error."""
        out = []
        if self.pending_changes != "block":
            out.append(f"pending_changes={self.pending_changes}: uncommitted candidate config will not stop an upgrade.")
        if self.failover_band > 0.5 or self.post_upgrade_band > 0.75:
            out.append("Session-count bands are very wide; a real traffic loss could pass unnoticed.")
        if self.ha_config_unsynced == "warn":
            out.append("ha_config_unsynced=warn: a pair whose passive config is not synchronized is planned anyway; when the active member "
                       "suspends, the passive takes over running its own, different configuration.")
        if self.blocked_peer == "warn":
            out.append("blocked_peer=warn: a member whose HA peer is blocked by a check is planned anyway; the active member hands traffic to "
                       "that peer while it reboots.")
        if self.allow_downgrade:
            out.append("allow_downgrade=true: firewalls running a newer release than the target are installed down to it (across feature "
                       "releases too; the newer release's configuration may not load on the older one).")
        if self.delete_unneeded_images:
            out.append("delete_unneeded_images=true: `prepare` deletes the PAN-OS images on a firewall that this upgrade does not need "
                       "(it keeps the running image, the base images and the target).")
        return out


_CHOICES = {
    "pending_changes": ("block", "warn", "ignore"),
    "restore_roles": ("leave", "restore"),
    "image_source": ("firewall", "panorama"),
    "panorama_compare": ("minor", "major"),
    "sync_to_peer": ("Disable", "Both", "UpgradeOnly"),
    "sync_content": ("never", "prepare"),
    "ha_config_unsynced": ("block", "warn"),
    "blocked_peer": ("block", "warn"),
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


def from_dict(data):
    known = {f.name: f.type for f in fields(Settings)}
    kwargs = {}
    for name, value in data.items():
        lowered = name.lower()
        if any(word in lowered for word in _SECRET_WORDS):
            raise SettingsError(f"{name}: secrets do not belong in settings; sign-in is set with --creds-file / --account (see `panupgrade help`)")
        if name not in known:
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
    for name in ("failover_band", "post_upgrade_band"):
        if not 0 < getattr(settings, name) <= 1:
            raise SettingsError(f"{name} must be between 0 and 1")
    for name in ("content_settle_s", "settle_after_failover_s", "settle_after_reboot_s"):
        if getattr(settings, name) < 0:
            raise SettingsError(f"{name} must not be negative")
    for name in ("spread_limit", "max_failures", "max_parallel", "sync_timeout_s", "busy_wait_s", "poll_interval_s"):
        if getattr(settings, name) < 1:
            raise SettingsError(f"{name} must be at least 1")
    return settings


def load(path=None, overrides=None):
    data = {}
    if path:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    for item in overrides or []:
        if "=" not in item:
            raise SettingsError(f"--set expects name=value, got {item!r}")
        name, raw = item.split("=", 1)
        data[name.strip()] = _parse_override(name.strip(), raw.strip())
    return from_dict(data)


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
