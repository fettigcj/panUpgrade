# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Plain data shared by sources, checks, planner and reports.

Vocabulary is panInventory's: pass / warn / fail / skip / info / error, with skip
kinds not_applicable / no_data / excluded. A check that could not decide is a
skip (no_data), never a pass and never a fail.
"""

from dataclasses import dataclass, field, fields

PASS, WARN, FAIL, SKIP, INFO, ERROR = "pass", "warn", "fail", "skip", "info", "error"
NOT_APPLICABLE, NO_DATA, EXCLUDED = "not_applicable", "no_data", "excluded"

# Cautions that follow from the chosen target, so every firewall gets the same words: the plan says them once for the fleet,
# naming the firewalls, and approving them is one acknowledgement ("fleet:<code>").
SAID_ONCE = {"target_cert_advisory": "Target is a build affected by a certificate-expiry advisory"}


@dataclass
class CheckResult:
    check: str
    status: str
    reason: str
    kind: str = ""          # skip kind, when status == skip
    ack: str = ""           # caution code the user must acknowledge to proceed (see SAID_ONCE for the ones said once for the fleet)
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        out = {"check": self.check, "status": self.status, "reason": self.reason}
        if self.kind:
            out["kind"] = self.kind
        if self.ack:
            out["ack"] = self.ack
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass
class Facts:
    """What a device reported. None always means 'not collected', never 'false'."""
    sw_version: str = None
    ha_mode: str = None            # none | active-passive | active-active
    ha_state: str = None           # active | passive | suspended | non-functional | ...
    peer_serial: str = None
    preemption: bool = None
    ha_session_sync: bool = None   # HA2: session table synchronised
    ha_config_sync: bool = None    # HA1: configuration synchronised
    ha_links_ok: bool = None
    disk_free_mb: int = None
    images_downloaded: list = None   # versions already on the device
    pending_changes: bool = None
    running_jobs: int = None
    session_count: int = None
    ntp_synced: bool = None
    licenses_ok: bool = None
    ha_content_compat: dict = None  # PAN-OS's own HA verdict per content type: Match | Mismatch | Unknown ("build","app","threat","av","url","iot")
    device_cert: dict = None        # {"status","validity","msg","not_valid_before","not_valid_after"} from `show device-certificate status`
    clock_utc: str = None           # the firewall's own clock as an ISO UTC time (None when it cannot be read)
    content_versions: dict = None   # {"app": "8545-7303", "threat": ..., "av": "0", "wildfire": "0", "url": ...}; "0" = not installed

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in fields(cls)}
        extra = set(data) - known
        if extra:
            raise ValueError(f"unknown fact(s): {', '.join(sorted(extra))}")
        return cls(**data)


@dataclass
class DeviceRef:
    """The Panorama-side view of one managed firewall."""
    serial: str
    hostname: str
    model: str
    sw_version: str
    connected: bool
    device_group: str = ""
    tags: list = field(default_factory=list)
    ha_state: str = ""       # as Panorama reports it; may be empty
    peer_serial: str = ""
