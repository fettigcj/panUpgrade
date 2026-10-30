"""The operator's go-ahead, as data: which plan, which devices, which cautions were acknowledged.

Nothing executes without one. It names devices (never "all"), is bound to one plan
by its hash, and must acknowledge every fleet caution and every caution on the
approved devices. An approval file records who said so and when; it is not a
credential and holds no secrets.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Approval:
    plan_hash: str
    serials: list
    acks: list = field(default_factory=list)      # "<serial|fleet>:<code>"
    approved_by: str = ""
    note: str = ""
    created: str = ""        # ISO time the approval was made; an old one is refused (setting approval_max_age_hours)
    snapshot: dict = field(default_factory=dict)   # the readable content the plan hash covers, kept so a refusal can say WHAT changed

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        extra = set(data) - {"plan_hash", "serials", "acks", "approved_by", "note", "created", "snapshot", "cautions_available", "affected_firewalls"}
        if extra:
            raise ValueError(f"unknown approval field(s): {', '.join(sorted(extra))}")
        data.pop("cautions_available", None)  # help lists in the template; acknowledging means copying into acks
        data.pop("affected_firewalls", None)
        return cls(**data)

    def to_dict(self):
        return dict(self.__dict__)

    def problems(self, plan, max_age_hours=None):
        out = []
        if self.plan_hash != plan["hash"]:
            changes = what_changed(self.snapshot, plan.get("snapshot"))
            if changes:
                out.append("the plan has changed since this approval was made. What is different now:")
                out += [f"    {line}" for line in changes]
                out.append("    Run `panupgrade prepare` again to review and approve the plan as it stands now.")
            else:
                out.append("something this approval was made for has changed since (which firewalls, their versions or HA roles, the planned "
                           "steps, or the cautions) and this approval file did not record enough to say which; run `panupgrade prepare` "
                           "again to review and approve the current plan")
        if max_age_hours and self.created:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(self.created)
                if age.total_seconds() > max_age_hours * 3600:
                    out.append(f"this approval is {age.total_seconds() / 3600:.0f} hours old (limit {max_age_hours}); "
                               f"run `panupgrade prepare` again to renew it")
            except ValueError:
                out.append(f"the approval's created time {self.created!r} is not a valid ISO time")
        if not self.serials:
            out.append("the approval names no devices")
        devices = {d["serial"]: d for d in plan["devices"]}
        for serial in self.serials:
            dev = devices.get(serial)
            if dev is None:
                out.append(f"{serial}: not in the plan")
            elif dev["verdict"] not in ("ready", "caution"):
                out.append(f"{serial}: verdict is {dev['verdict']}, not executable")
        for c in plan["cautions"]:
            if c["scope"] == "fleet" or c["scope"] in self.serials:
                if f"{c['scope']}:{c['code']}" not in self.acks:
                    out.append(f"unacknowledged caution {c['scope']}:{c['code']}: {c['text']}")
        return out


def what_changed(old, new):
    """Plain-language differences between two plan snapshots (the content a plan hash covers). Empty if either is missing."""
    if not old or not new:
        return []
    old, new = json.loads(json.dumps(old)), json.loads(json.dumps(new))       # one form for both: a saved file holds lists where a live plan holds tuples
    names = {**old.get("hostnames", {}), **new.get("hostnames", {})}

    def who(serial):
        return f"{names[serial]} ({serial})" if serial in names else serial

    out = []
    for key, label in (("target", "target version"), ("panorama_version", "Panorama version")):
        if old.get(key) != new.get(key):
            out.append(f"{label}: {old.get(key)} -> {new.get(key)}")
    for key in sorted(set(old.get("settings", {})) | set(new.get("settings", {}))):
        a, b = old.get("settings", {}).get(key), new.get("settings", {}).get(key)
        if a != b:
            out.append(f"setting {key}: {a} -> {b}")
    before = {d["serial"]: d for d in old.get("devices", [])}
    after = {d["serial"]: d for d in new.get("devices", [])}
    for serial in sorted(set(before) - set(after)):
        out.append(f"{who(serial)}: no longer in the plan")
    for serial in sorted(set(after) - set(before)):
        out.append(f"{who(serial)}: newly in the plan (verdict {after[serial]['verdict']})")
    for serial in sorted(set(before) & set(after)):
        a, b = before[serial], after[serial]
        for key, label in (("version", "running version"), ("ha_mode", "HA mode"), ("ha_state", "HA role"), ("peer", "HA peer"),
                           ("verdict", "verdict")):
            if a.get(key) != b.get(key):
                out.append(f"{who(serial)}: {label} {a.get(key)} -> {b.get(key)}")
        if a.get("steps") != b.get("steps"):
            out.append(f"{who(serial)}: planned steps differ: was {', '.join(s[0] for s in a.get('steps', [])) or 'none'}; "
                       f"now {', '.join(s[0] for s in b.get('steps', [])) or 'none'}")
        for code in sorted(set(a.get("acks", [])) - set(b.get("acks", []))):
            out.append(f"{who(serial)}: caution [{code}] no longer applies")
        for code in sorted(set(b.get("acks", [])) - set(a.get("acks", []))):
            out.append(f"{who(serial)}: NEW caution [{code}], not acknowledged by the approval")
    if old.get("order") != new.get("order"):
        out.append("the order or grouping of the firewalls differs")
    for code in sorted(set(old.get("fleet_acks", [])) ^ set(new.get("fleet_acks", []))):
        out.append(f"fleet-wide caution [{code}] " + ("no longer applies" if code in old.get("fleet_acks", []) else "is new"))
    return out
