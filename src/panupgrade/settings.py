# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""User-tunable settings: one plain TOML file, strict types, safe defaults.

Secrets never appear here. Credentials are *paths* to files (keys ending in
``_file``); any other key that looks like a secret is rejected.
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
    # Panorama should stay within this many MAJOR versions of the firewalls it manages (a rule of thumb from experience
    # with schema conversion). A firewall further behind gets "advise upgrade"; it never blocks that firewall's upgrade.
    spread_limit: int = 2
    # How "Panorama is older than the firewall's target" is judged: feature (10.2 vs 11.0) | full (every number)
    panorama_compare: str = "feature"
    # Batch handling
    max_failures: int = 5
    max_parallel: int = 1               # HA pairs (or standalone firewalls) worked on at the same time; a pair's members stay in series
    api_rate_per_s: float = 5.0         # polite ceiling on requests per second to Panorama, shared by every worker
    # Active-to-peer config sync ("sync to peer"): never (report only) | prepare (a separate pre-maintenance
    # `prepare` task does it) | upgrade (the upgrade run does it first, for each pair that needs it).
    # Production actives should always be syncing to the passive; the sync forces the passive to match what is running.
    sync_to_peer: str = "prepare"
    sync_timeout_s: int = 600
    # A pair whose passive member's config is NOT synchronized. The risk is exactly one moment: the ACTIVE member suspends and the
    # upgraded passive takes over running its own, different configuration. block (the default): refuse to plan such a pair at all,
    # so nobody runs into it unread; with sync_to_peer = prepare a healthy pair is synced first and never reaches this. caution:
    # that moment is flagged on the active member with a plain-words message, to be accepted when the batch is prepared; the
    # passive's own upgrade is unaffected. Ignored when sync_to_peer = upgrade, which forces the sync instead.
    ha_config_unsynced: str = "block"
    # Dynamic content (the cloud-delivered security services: applications/threats, antivirus, WildFire). HA peers should run
    # the same versions, and a mismatch gates the config sync. never | prepare (install the latest on both members as part of the
    # approved preparation; the approval prompt lists it and says it is outside the customer's own content schedule).
    content_settle_s: int = 90      # after "check now" on both members, how long to wait for PAN-OS's own HA verdict to turn to Match before installing
    sync_content: str = "prepare"
    # Installing an older release than the one running. Same feature release only; each one needs an acknowledgement.
    allow_downgrade: bool = False
    # Going down across a feature release (11.0 -> 10.2). Needs allow_downgrade too. A last-resort override: the newer
    # release's configuration may not load on the older one, so a saved config of the older release is the way back.
    allow_cross_feature_downgrade: bool = False
    # What to do about a pending (uncommitted) candidate config: block | warn | ignore
    pending_changes: str = "block"
    # HA roles after both members are upgraded: leave | restore | pause
    restore_roles: str = "leave"
    # Who fetches images: firewall (default, downloads from the vendor) | panorama
    image_source: str = "firewall"
    # Placeholders, conservative; tune them from your own first-batch evidence
    min_free_disk_mb: int = 4096
    download_timeout_s: int = 3600
    install_timeout_s: int = 3600
    reboot_timeout_s: int = 1800
    poll_interval_s: int = 15          # how often a long job or a rebooting firewall is checked
    backup_dir: str = "backups"        # where verified config backups are written
    approval_max_age_hours: int = 168  # an approval older than this (a week) is refused (0 = never expires)
    # Credential file paths only
    plan_credentials_file: str = ""
    apply_credentials_file: str = ""

    def loosening_warnings(self):
        """Plain-language cautions for settings that make a run riskier. Never an error."""
        out = []
        if self.pending_changes != "block":
            out.append(f"pending_changes={self.pending_changes}: uncommitted candidate config will not stop an upgrade.")
        if self.failover_band > 0.5 or self.post_upgrade_band > 0.75:
            out.append("Session-count bands are very wide; a real traffic loss could pass unnoticed.")
        if self.ha_config_unsynced == "caution":
            out.append("ha_config_unsynced=caution: a pair whose passive config is not synchronized is planned anyway; when the active member "
                       "suspends, the passive takes over running its own, different configuration.")
        if self.sync_to_peer == "upgrade":
            out.append(f"sync_to_peer={self.sync_to_peer}: the passive member's configuration is overwritten with the active member's.")
        if self.allow_downgrade:
            out.append("allow_downgrade=true: firewalls running a newer release than the target are installed down to it.")
        if self.allow_cross_feature_downgrade:
            out.append("allow_cross_feature_downgrade=true: firewalls may be installed down across feature releases; "
                       "the newer release's configuration may not load on the older one.")
        if self.max_parallel > 1:
            out.append(f"max_parallel={self.max_parallel}: several HA pairs/devices change at once.")
        if (self.plan_credentials_file and self.plan_credentials_file == self.apply_credentials_file):
            out.append("The read-only and apply credentials are the same file; a plan run holds change-capable access.")
        return out


_CHOICES = {
    "pending_changes": ("block", "warn", "ignore"),
    "restore_roles": ("leave", "restore", "pause"),
    "image_source": ("firewall", "panorama"),
    "panorama_compare": ("feature", "full"),
    "sync_to_peer": ("never", "prepare", "upgrade"),
    "sync_content": ("never", "prepare"),
    "ha_config_unsynced": ("block", "caution"),
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
        if any(word in lowered for word in _SECRET_WORDS) and not lowered.endswith("_file"):
            raise SettingsError(f"{name}: secrets do not belong in settings; give a *_file path instead")
        if name not in known:
            raise SettingsError(f"unknown setting {name!r}")
        kwargs[name] = _coerce(name, value, known[name])
    settings = Settings(**kwargs)
    for name, choices in _CHOICES.items():
        if getattr(settings, name) not in choices:
            raise SettingsError(f"{name} must be one of {', '.join(choices)}")
    if settings.allow_cross_feature_downgrade and not settings.allow_downgrade:
        raise SettingsError("allow_cross_feature_downgrade only means something together with allow_downgrade = true")
    if settings.api_rate_per_s <= 0:
        raise SettingsError("api_rate_per_s must be above 0")
    for name in ("failover_band", "post_upgrade_band"):
        if not 0 < getattr(settings, name) <= 1:
            raise SettingsError(f"{name} must be between 0 and 1")
    if settings.content_settle_s < 0:
        raise SettingsError("content_settle_s must not be negative")
    for name in ("spread_limit", "max_failures", "max_parallel", "sync_timeout_s", "poll_interval_s"):
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
