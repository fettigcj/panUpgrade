# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""What a run has changed on a firewall and not yet put back, kept in a small file of the profile, OUTSIDE any run's journal.

A run's journal knows only that run. A run that was stopped, or whose restore a firewall refused (it must reboot before it takes a commit), would
leave a firewall changed with nothing for the NEXT run to find: the next run would read the changed values as the firewall's own and put those
back. The ledger is what the next run reads first. An entry is written BEFORE the change is made and removed only when the put-back is verified.

Two kinds of entry per firewall: ``window`` (the TCP settings of the asymmetry window: the original values) and ``drain`` (the interface management
profile taken out for a load balancer: what it was).
"""

import json
import os
import threading
import time
from pathlib import Path


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def entries(self):
        """{serial: {"window": {...}, "drain": {...}}} of everything not yet put back."""
        with self._lock:
            return self._load()

    def _set(self, serial, kind, value):
        with self._lock:
            data = self._load()
            data.setdefault(serial, {})[kind] = {**value, "since": time.strftime("%Y-%m-%d %H:%M:%S %z")}
            self._save(data)

    def _clear(self, serial, kind):
        with self._lock:
            data = self._load()
            if serial in data and kind in data[serial]:
                del data[serial][kind]
                if not data[serial]:
                    del data[serial]
                self._save(data)

    def window(self, serial):
        return self.entries().get(serial, {}).get("window")

    def set_window(self, serial, original, name, journal):
        self._set(serial, "window", {"original": original, "window": name, "journal": str(journal)})

    def clear_window(self, serial):
        self._clear(serial, "window")

    def drain(self, serial):
        return self.entries().get(serial, {}).get("drain")

    def set_drain(self, serial, detail, journal):
        self._set(serial, "drain", {**detail, "journal": str(journal)})

    def clear_drain(self, serial):
        self._clear(serial, "drain")


def describe(entries, names=None):
    """Plain-language lines for what is left changed."""
    names = names or {}
    lines = []
    for serial, kinds in sorted(entries.items()):
        who = f"{names[serial]} ({serial})" if serial in names else serial
        if "window" in kinds:
            lines.append(f"{who}: still set to accept asymmetric traffic (left by {kinds['window'].get('window', 'a window')}, {kinds['window'].get('since', '')})")
        if "drain" in kinds:
            lines.append(f"{who}: the interface management profile {kinds['drain'].get('profile')!r} may still be drained ({kinds['drain'].get('since', '')})")
    return lines
