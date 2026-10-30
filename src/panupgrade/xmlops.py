# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Read-only device queries as parsers over an ``op`` callable, plus a canned-XML double.

``op(command)`` takes an operational command as free text (for example
``"show system info"``) and returns the parsed ``<response>`` element. ``panupgrade_live``
supplies the real ``op``; nothing here touches the network, so everything below runs offline.

The canned XML in tests is written from the shape the Upgrade Assurance source
parses, NOT from a recording. Every response shape is UNVERIFIED until it is
recorded from a real firewall; a parser that cannot find what it needs reports "no data"
(None) and never guesses.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .model import DeviceRef, Facts
from .sources import Source, SourceError

READ_ONLY_PREFIXES = ("show ", "check ", "request system software check", "request system software info",
                      "request content upgrade check", "request anti-virus upgrade check", "request wildfire upgrade check")


class XmlOpError(Exception):
    """The device answered, but not with a usable success response."""


class UnscriptedCommand(AssertionError):
    """The canned double was asked for a command nobody scripted."""


class CannedOp:
    """Offline stand-in for a device's ``op``. Unscripted commands fail loudly."""

    def __init__(self, responses):
        self._responses = dict(responses)
        self.calls = []

    def __call__(self, command):
        self.calls.append(command)
        if command not in self._responses:
            raise UnscriptedCommand(f"unscripted command: {command!r}")
        item = self._responses[command]
        if isinstance(item, Exception):
            raise item
        return ET.fromstring(item)


def ok(result_xml):
    return f"<response status='success'><result>{result_xml}</result></response>"


def _result(response, command):
    if response.get("status") != "success":
        raise XmlOpError(f"{command}: device reported status {response.get('status')!r}")
    node = response.find("result")
    if node is None:
        raise XmlOpError(f"{command}: no result element")
    return node


def _text(node, path):
    found = node.find(path)
    return found.text.strip() if found is not None and found.text else None


def _yes(value):
    return None if value is None else value.lower() == "yes"


# ----------------------------------------------------------------- parsers

def parse_version(op):
    node = _result(op("show system info"), "show system info")
    return _text(node, ".//sw-version")


def parse_pending(op):
    node = _result(op("check pending-changes"), "check pending-changes")
    return _yes((node.text or "").strip() or None)


def parse_running_jobs(op):
    node = _result(op("show jobs all"), "show jobs all")
    jobs = node.findall(".//job")
    return sum(1 for j in jobs if (_text(j, "status") or "") != "FIN")


def parse_session_count(op):
    node = _result(op("show session info"), "show session info")
    value = _text(node, ".//num-active")
    return int(value) if value is not None and value.isdigit() else None


CONTENT_TAGS = {"app": "app-version", "threat": "threat-version", "av": "av-version", "wildfire": "wildfire-version",
                "url": "url-filtering-version"}


def parse_content_versions(op):
    """The dynamic-content versions a firewall reports in ``show system info`` (None for any it does not report)."""
    node = _result(op("show system info"), "show system info")
    return {key: _text(node, f".//{tag}") for key, tag in CONTENT_TAGS.items()}


def parse_device_cert(op):
    """`show device-certificate status`. Real replies (2026-10-29): validity is 'Valid' or 'Not yet valid' (a clock that runs behind the
    certificate's start time), with status 'success' and a message; a firewall without one answers with an error message."""
    node = _result(op("show device-certificate status"), "show device-certificate status")
    cert = node.find("device-certificate")
    if cert is None:
        return {"status": "none", "validity": None, "msg": " ".join((node.text or "").split())[:160] or None}
    return {"status": _text(cert, "status"), "validity": _text(cert, "validity"), "msg": _text(cert, "msg"),
            "not_valid_before": _text(cert, "not_valid_before"), "not_valid_after": _text(cert, "not_valid_after")}


_TZ_HOURS = {"UTC": 0, "GMT": 0, "PDT": -7, "PST": -8, "MDT": -6, "MST": -7, "CDT": -5, "CST": -6, "EDT": -4, "EST": -5}


def parse_clock_text(text):
    """'Thu Oct 29 09:21:23 PDT 2026' -> ISO UTC string, or None if the zone is not one we know (never guess an offset)."""
    m = re.search(r"(\w{3}) +(\d+) (\d+):(\d+):(\d+) (\w+) (\d{4})", text or "")
    if not m or m.group(6) not in _TZ_HOURS:
        return None
    month = datetime.strptime(m.group(1), "%b").month
    local = datetime(int(m.group(7)), month, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
    return (local - timedelta(hours=_TZ_HOURS[m.group(6)])).replace(tzinfo=timezone.utc).isoformat()


def parse_clock(op):
    node = _result(op("show clock"), "show clock")
    return parse_clock_text(node.text)


def parse_images_downloaded(op):
    """Versions whose image is on the device (downloaded = yes). Shape seen on 10.2.x: entries under sw-updates/versions."""
    node = _result(op("request system software info"), "request system software info")
    return sorted(_text(e, "version") for e in node.iter("entry") if _text(e, "downloaded") == "yes" and _text(e, "version"))


_UNITS = {"T": 1024 * 1024, "G": 1024, "M": 1, "K": 1 / 1024}


def parse_free_mb(op, mount="/opt/panrepo"):
    """Free MB on one mount point from the df-style text of ``show system disk-space``."""
    node = _result(op("show system disk-space"), "show system disk-space")
    for line in (node.text or "").splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 6 and cols[-1] == mount:
            avail = cols[3]
            unit = _UNITS.get(avail[-1])
            try:
                return int(float(avail[:-1]) * unit) if unit else None
            except ValueError:
                return None
    return None


def parse_ha(op):
    """dict of HA facts; keys absent from the response are simply missing (no data)."""
    node = _result(op("show high-availability state"), "show high-availability state")
    if _yes(_text(node, "enabled")) is not True:
        return {"ha_mode": "none"}
    group = node.find("group")
    if group is None:
        return {}
    out = {}
    mode = _text(group, "mode")
    if mode:
        out["ha_mode"] = {"Active-Passive": "active-passive", "Active-Active": "active-active"}.get(mode, mode.lower())
    out["ha_state"] = _text(group, "local-info/state")
    out["peer_state"] = _text(group, "peer-info/state")
    if _text(group, "running-sync"):
        out["ha_config_sync"] = _text(group, "running-sync") == "synchronized"
    # Verified against recordings from PAN-OS 10.2.1 and 10.2.11-h1 (2026-10-29): the session-sync state is under local-info.
    if _text(group, "local-info/state-sync"):
        out["ha_session_sync"] = _text(group, "local-info/state-sync").lower() == "complete"
    # Link health: the HA1 and HA2 connections to the peer both report "up" (the backup link is informational).
    links = [_text(group, "peer-info/conn-ha1/conn-status"), _text(group, "peer-info/conn-ha2/conn-status")]
    if all(links):
        out["ha_links_ok"] = all(v.lower() == "up" for v in links)
    if _text(group, "local-info/preemptive"):
        out["preemption"] = _yes(_text(group, "local-info/preemptive"))
    # PAN-OS's own sanity check on whether the members' content can coexist. Seen 2026-10-29: "Match" on pairs whose config sync
    # works, "Unknown" on pairs whose config sync is refused, "Mismatch" for the PAN-DB URL version (never judged: the standby usually cannot reach the URL cloud).
    compat = {k: _text(group, f"local-info/{k}-compat") for k in ("build", "app", "threat", "av", "url", "iot")}
    if any(compat.values()):
        out["ha_content_compat"] = compat
    return out


def collect_facts(op, peer_serial=None):
    """Run every read-only query; whatever fails or is missing stays None. Returns (Facts, problems)."""
    facts, problems = Facts(), []

    def attempt(label, fn, apply):
        try:
            apply(fn(op))
        except (XmlOpError, ET.ParseError) as exc:
            problems.append(f"{label}: {exc}")

    attempt("version", parse_version, lambda v: setattr(facts, "sw_version", v))
    attempt("pending changes", parse_pending, lambda v: setattr(facts, "pending_changes", v))
    attempt("jobs", parse_running_jobs, lambda v: setattr(facts, "running_jobs", v))
    attempt("sessions", parse_session_count, lambda v: setattr(facts, "session_count", v))
    attempt("disk", parse_free_mb, lambda v: setattr(facts, "disk_free_mb", v))
    attempt("images", parse_images_downloaded, lambda v: setattr(facts, "images_downloaded", v))
    attempt("content", parse_content_versions, lambda v: setattr(facts, "content_versions", v))
    attempt("device certificate", parse_device_cert, lambda v: setattr(facts, "device_cert", v))
    attempt("clock", parse_clock, lambda v: setattr(facts, "clock_utc", v))

    def apply_ha(ha):
        facts.ha_mode = ha.get("ha_mode")
        facts.ha_state = ha.get("ha_state")
        facts.ha_config_sync = ha.get("ha_config_sync")
        facts.ha_session_sync = ha.get("ha_session_sync")
        facts.ha_links_ok = ha.get("ha_links_ok")
        facts.ha_content_compat = ha.get("ha_content_compat")
        facts.preemption = ha.get("preemption")
        if facts.ha_mode not in (None, "none"):
            facts.peer_serial = peer_serial

    attempt("high availability", parse_ha, apply_ha)
    return facts, problems


class OpSource(Source):
    """A Source over per-device ``op`` callables. Offline by itself; a live adapter is a later phase."""
    kind = "op"

    def __init__(self, panorama_version, refs, op_for, panorama_clock=None):
        self._pano, self._refs, self._op_for = panorama_version, list(refs), op_for
        self._pano_clock = panorama_clock

    def panorama_version(self):
        return self._pano

    def panorama_clock(self):
        return self._pano_clock

    def list_devices(self):
        return list(self._refs)

    def facts(self, serial):
        ref = next((r for r in self._refs if r.serial == serial), None)
        if ref is None:
            raise SourceError(f"{serial} is not in the fleet")
        facts, problems = collect_facts(self._op_for(serial), ref.peer_serial or None)
        if facts.sw_version is None and problems:
            raise SourceError("; ".join(problems))
        return facts
