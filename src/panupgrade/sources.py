# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Where facts come from.

FakeFleet is a JSON file describing an invented fleet, so the whole engine can be
exercised with no firewall. The live, Panorama-backed source lives in ``panupgrade_live``.
"""

import json

from .model import DeviceRef, Facts


class SourceError(Exception):
    """A device could not be read. The planner turns this into 'no data'."""


class Source:
    kind = "abstract"

    def panorama_version(self):
        raise NotImplementedError

    def list_devices(self):
        raise NotImplementedError

    def facts(self, serial):
        raise NotImplementedError

    def panorama_clock(self):
        """Panorama's own clock as an ISO UTC time, or None; the yardstick for each firewall's clock."""
        return None

    def available_versions(self, serial):
        """The PAN-OS versions this firewall lists as available, newest first: [{version, downloaded, current, latest, released}]."""
        raise SourceError("this source cannot list the versions a firewall offers")


class FakeFleet(Source):
    kind = "fake"

    def __init__(self, data):
        self._pano = data.get("panorama", {})
        self._devices = {}
        order = []
        for entry in data.get("devices", []):
            serial = entry["serial"]
            if serial in self._devices:
                raise ValueError(f"duplicate serial {serial} in fleet file")
            self._devices[serial] = entry
            order.append(serial)
        self._order = order

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    def panorama_version(self):
        return self._pano.get("version")

    def panorama_clock(self):
        return self._pano.get("clock_utc")

    def list_devices(self):
        refs = []
        for serial in self._order:
            e = self._devices[serial]
            refs.append(DeviceRef(
                serial=serial,
                hostname=e.get("hostname", serial),
                model=e.get("model", ""),
                sw_version=e.get("sw_version", ""),
                connected=e.get("connected", True),
                device_group=e.get("device_group", ""),
                tags=list(e.get("tags", [])),
                ha_state=e.get("ha_state", ""),
                peer_serial=e.get("peer_serial", ""),
            ))
        return refs

    def available_versions(self, serial):
        entry = self._devices.get(serial)
        if entry is None:
            raise SourceError(f"{serial} is not in the fleet")
        listed = entry.get("software_versions")
        if listed is None:
            raise SourceError("the fleet file lists no software versions for this firewall")
        return [{"version": v, "downloaded": False, "current": v == entry.get("sw_version"), "latest": False, "released": ""} if isinstance(v, str) else v
                for v in listed]

    def facts(self, serial):
        entry = self._devices.get(serial)
        if entry is None:
            raise SourceError(f"{serial} is not in the fleet")
        if not entry.get("connected", True):
            raise SourceError("device is not connected to Panorama")
        if entry.get("collect_error"):
            raise SourceError(entry["collect_error"])
        try:
            return Facts.from_dict(entry.get("facts", {}))
        except (TypeError, ValueError) as exc:
            raise SourceError(f"unreadable facts: {exc}") from None


def snapshot(source, include=None):
    """The source's current state in the fleet-file shape (what FakeFleet reads), so a rehearsal starts from live facts.

    Devices that cannot be read keep ``connected`` as reported and carry a ``collect_error``; nothing is invented.
    """
    from dataclasses import asdict
    from . import progress
    progress.phase("Asking Panorama for the list of firewalls")
    refs = source.list_devices()
    total = sum(1 for r in refs if r.connected and (include is None or r.serial in include))
    devices, read = [], 0
    for ref in refs:
        entry = {"serial": ref.serial, "hostname": ref.hostname, "model": ref.model, "sw_version": ref.sw_version,
                 "connected": ref.connected, "device_group": ref.device_group, "tags": list(ref.tags),
                 "ha_state": ref.ha_state, "peer_serial": ref.peer_serial}
        if include is not None and ref.serial not in include:
            entry["facts"] = {}          # not selected and not a selected device's HA peer: never read
        elif ref.connected:
            read += 1
            progress.phase(f"Reading firewall {read} of {total}: {ref.hostname}")
            try:
                entry["facts"] = {k: v for k, v in asdict(source.facts(ref.serial)).items() if v is not None}
            except SourceError as exc:
                entry["collect_error"], entry["facts"] = str(exc), {}
        else:
            entry["facts"] = {}
        devices.append(entry)
    progress.phase("Reading Panorama's version")
    return {"panorama": {"version": source.panorama_version()}, "devices": devices}
