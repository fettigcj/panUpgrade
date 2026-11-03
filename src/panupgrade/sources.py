# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Where facts come from.

FakeFleet is a JSON file describing an invented fleet, so the whole engine can be
exercised with no firewall. The live, Panorama-backed source lives in ``panupgrade_live``.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def read_facts(source, serials, workers=1, announce=None):
    """Read the facts of several firewalls, up to ``workers`` at a time. Returns {serial: Facts, or the SourceError that stopped it}.

    The firewalls are independent, so this is the one place the slow part of a fleet read is spread out. ``announce(done, total, serial)``
    is called on the calling thread as each one finishes (the worker threads have no screen of their own). With one worker, or one
    firewall, nothing is threaded."""
    serials = list(dict.fromkeys(serials))
    found = {}

    def one(serial):
        try:
            return serial, source.facts(serial)
        except SourceError as exc:
            return serial, exc

    if workers <= 1 or len(serials) <= 1:
        for done, serial in enumerate(serials, 1):
            if announce:
                announce(done, len(serials), serial)
            found[serial] = one(serial)[1]
        return found
    pool = ThreadPoolExecutor(max_workers=min(workers, len(serials)), thread_name_prefix="read")
    futures = [pool.submit(one, s) for s in serials]
    try:
        for done, future in enumerate(as_completed(futures), 1):
            serial, result = future.result()
            found[serial] = result
            if announce:
                announce(done, len(serials), serial)
    except BaseException:                   # Ctrl-C: queued reads are dropped, the ones in flight finish
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True)
        raise
    pool.shutdown()
    return found


def snapshot(source, include=None, workers=1):
    """The source's current state in the fleet-file shape (what FakeFleet reads), so a rehearsal starts from live facts.

    Devices that cannot be read keep ``connected`` as reported and carry a ``collect_error``; nothing is invented.
    """
    from dataclasses import asdict
    from . import progress
    progress.phase("Asking Panorama for the list of firewalls")
    refs = source.list_devices()
    wanted = [r.serial for r in refs if r.connected and (include is None or r.serial in include)]
    names = {r.serial: r.hostname for r in refs}
    got = read_facts(source, wanted, workers,
                     lambda done, count, serial: progress.phase(f"Reading firewall {done} of {count}: {names[serial]}"))
    devices = []
    for ref in refs:
        entry = {"serial": ref.serial, "hostname": ref.hostname, "model": ref.model, "sw_version": ref.sw_version,
                 "connected": ref.connected, "device_group": ref.device_group, "tags": list(ref.tags),
                 "ha_state": ref.ha_state, "peer_serial": ref.peer_serial}
        if ref.serial in got:
            result = got[ref.serial]
            if isinstance(result, SourceError):
                entry["collect_error"], entry["facts"] = str(result), {}
            else:
                entry["facts"] = {k: v for k, v in asdict(result).items() if v is not None}
        else:
            entry["facts"] = {}          # not selected and not a selected device's HA peer (never read), or not connected
        devices.append(entry)
    progress.phase("Reading Panorama's version")
    return {"panorama": {"version": source.panorama_version()}, "devices": devices}
