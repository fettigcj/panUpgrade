# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The preemption window (setting ``disable_preemption``, default off).

An HA pair whose members have preemption on hands the active role back to the preferred member as soon as that member comes back. During an upgrade
that can be too soon: the member has only just rebooted, its sessions and content may not have settled, and a second fail-over lands in the middle of
the batch. With ``disable_preemption`` on, before the first member of a pair is touched each member's own ``preemptive`` value is recorded and set to
``no`` (committed on the firewall), and it is put back when the pair is finished, whether it succeeded or not. The value recorded is the one in the
firewall's LOCAL configuration: "not set" is a value too, put back by deleting the node (a Panorama template's value then applies again).

Every change and every restore is a journal step ("preemption open", "preemption restore"), the value is remembered in the ledger BEFORE it is changed
and forgotten only when the put-back is verified, and a firewall that could not be put back is reported loudly at the end.

Consequence to know about: with preemption off for the window, the pair does NOT return to its original roles by itself; when the window closes preemption
is on again, and the member that is preferred takes the active role back at that moment (a fail-over). ``restore_roles`` is unaffected.
"""

import threading

from .executor import BackendError, CommitRefused

OPEN, RESTORE = "preemption open", "preemption restore"


def _ledger(journal):
    return getattr(journal, "ledger", None)


class Preemption:
    """The preemption windows of one run. ``begin(unit)`` before a unit is run and ``end(unit)`` after it; ``close_all()`` is the safety net."""

    def __init__(self, plan, approved, backend, journal, say=print, settings=None):
        self.backend, self.journal, self.say, self.settings = backend, journal, say, settings
        self.windows, self.by_serial, self.unrestored = [], {}, []
        for spec in plan.get("preemption") or []:
            planned = [s for s in spec["serials"] if s in approved]
            if not planned:
                continue
            units = [u for u in plan["order"] if any(s in planned for s in u)]
            window = {"name": spec["name"], "serials": planned, "left": len(units), "state": "new", "reason": "", "lock": threading.Lock(), "opened": []}
            self.windows.append(window)
            for s in planned:
                self.by_serial[s] = window

    def _of(self, unit):
        found = []
        for serial in unit:
            window = self.by_serial.get(serial)
            if window is not None and window not in found:
                found.append(window)
        return found

    def begin(self, unit):
        """"" when every window the unit needs is open, else why it could not be opened (nothing was left changed)."""
        for window in self._of(unit):
            with window["lock"]:
                if window["state"] == "new":
                    window["state"], window["reason"] = self._open(window)
                if window["state"] == "failed":
                    return f"could not turn preemption off for {window['name']}: {window['reason']}"
        return ""

    def end(self, unit):
        for window in self._of(unit):
            with window["lock"]:
                window["left"] -= 1
                if window["left"] <= 0:
                    self._close(window)

    def close_all(self):
        for window in self.windows:
            with window["lock"]:
                self._close(window)

    def _open(self, window):
        try:
            for serial in window["serials"]:
                self._open_one(serial, window)
        except BackendError as exc:
            self._close(window)                                      # put back whatever was already changed
            return "failed", str(exc)
        return "open", ""

    def _open_one(self, serial, window):
        from .windows import clear_candidate
        held = _ledger(self.journal) and _ledger(self.journal).preempt(serial)
        if held:                                                     # an earlier run turned it off and never put it back: keep THAT record
            window["opened"].append((serial, held["original"]))
            self.say(f"{serial}: preemption was already turned off by an earlier run; keeping the value recorded then")
            return
        clear_candidate(self.backend, self.journal, serial, self.settings, "the commit that turns preemption off")
        original = self.backend.preemption_config(serial)
        self.journal.record(serial, OPEN, "started", {"original": original, "window": window["name"]})
        if _ledger(self.journal):
            _ledger(self.journal).set_preempt(serial, original, window["name"], self.journal.path)
        window["opened"].append((serial, original))                  # from here on a restore is attempted, even if the change below fails half way
        try:
            self.backend.set_preemption(serial, "no")
            if self.backend.state(serial).get("preemption") is not False:
                raise BackendError(f"{serial}: after the change the firewall still reports preemption on")
        except CommitRefused as exc:                                 # nothing was committed and the edits were taken back: nothing to restore
            window["opened"].remove((serial, original))
            if _ledger(self.journal):
                _ledger(self.journal).clear_preempt(serial)
            self.journal.record(serial, OPEN, "failed", {"original": original, "error": str(exc), "changed": False})
            raise
        except BackendError as exc:
            self.journal.record(serial, OPEN, "failed", {"original": original, "error": str(exc)})
            raise
        self.journal.record(serial, OPEN, "ok", {"original": original, "window": window["name"]})

    def _close(self, window):
        opened, window["opened"] = window["opened"], []
        for serial, original in opened:
            error = restore_one(serial, original, self.backend, self.journal)
            if error:
                entry = {"serial": serial, "window": window["name"], "original": original, "error": error}
                self.unrestored.append(entry)
                self.say(f"ALERT {serial}: could not put preemption back (it was {_word(original)} in its own configuration): {error}. Fix it by hand.")


def _word(original):
    return "not set" if original is None else original


def restore_one(serial, original, backend, journal, leftover=False):
    """Put one firewall's own ``preemptive`` value back and check both the configuration and the value in force. "" when done, else why not."""
    extra = {"leftover": True} if leftover else {}
    journal.record(serial, RESTORE, "started", {"original": original, **extra})
    try:
        backend.set_preemption(serial, original)
        now = backend.preemption_config(serial)
        if now != original:
            raise BackendError(f"{serial}: after the restore the firewall reads {_word(now)}, not {_word(original)}")
    except BackendError as exc:
        journal.record(serial, RESTORE, "failed", {"original": original, "error": str(exc), **extra})
        return str(exc)
    journal.record(serial, RESTORE, "ok", {"original": original, "now": now, **extra})
    if _ledger(journal):
        _ledger(journal).clear_preempt(serial)
    return ""
