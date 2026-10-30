# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Append-only record of an execution, one JSON object per line.

Every step writes ``started`` before it acts and ``ok`` or ``failed`` after. On
resume a step that has ``started`` and nothing else is *in doubt* (we do not know
whether a reboot or install happened), and is never replayed automatically.
The first line pins the journal to one plan by its hash.
"""

import json
import os
import threading
from datetime import datetime, timezone


class JournalError(Exception):
    pass


class Journal:
    def __init__(self, path, plan_hash, events):
        self.path, self.plan_hash, self.events = path, plan_hash, events
        self.listener = None     # optional callable(record): the console prints each event as it is written
        self._lock = threading.RLock()   # several workers write; one at a time keeps the file, the list and the screen in the same order

    @classmethod
    def create(cls, path, plan_hash):
        if os.path.exists(path):
            raise JournalError(f"{path} already exists: that run has already started. Add --resume to continue it "
                               f"(finished steps are skipped), or leave --journal out and a new file is named for you")
        journal = cls(path, plan_hash, [])
        journal._write({"type": "header", "plan_hash": plan_hash})
        return journal

    @classmethod
    def resume(cls, path, plan_hash):
        events, header = [], None
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        for number, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if number == len(lines) - 1:
                    break  # a torn final line from a crash mid-write
                raise JournalError(f"{path} line {number + 1} is corrupt") from None
            if record.get("type") == "header":
                header = record
            else:
                events.append(record)
        if header is None:
            raise JournalError(f"{path} has no header")
        if header["plan_hash"] != plan_hash:
            raise JournalError("this journal belongs to a different plan; refusing to resume")
        return cls(path, plan_hash, events)

    def _write(self, record):
        with self._lock:
            record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if record["type"] != "header":
                self.events.append(record)
                if self.listener:
                    self.listener(record)

    def record(self, serial, step, state, detail=None):
        """state: started | ok | failed | note"""
        self._write({"type": "step", "serial": serial, "step": step, "state": state, "detail": detail or {}})

    # ---- reading back

    def steps_of(self, serial):
        return [e for e in self.events if e.get("serial") == serial and e["type"] == "step"]

    def last_state(self, serial, step):
        state = None
        for e in self.steps_of(serial):
            if e["step"] == step and e["state"] in ("started", "ok", "failed"):
                state = e["state"]
        return state

    def detail(self, serial, step):
        found = {}
        for e in self.steps_of(serial):
            if e["step"] == step and e["state"] == "ok":
                found = e["detail"]
        return found
