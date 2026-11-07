# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Two things the plan can ask of a run besides the steps of each firewall:

* the asymmetry window: while a pair or a stand-alone group is being upgraded, its members accept traffic that LOOKS asymmetric
  (``tcp-reject-non-syn`` off, in force and in the configuration, ``asymmetric-path`` bypass). The member that takes over has not seen the handshakes the other member
  was in the middle of, and a stand-alone group has no HA2 link at all, so without this those flows are dropped. Each firewall's
  own values are recorded BEFORE they are changed and put back when the whole pair / group is finished, successful or not. Every
  change and every restore is a journal step ("asymmetry open", "asymmetry restore"), and a firewall that could not be put back is
  reported, loudly, in the run's result.
* the group gate: how many members of one stand-alone group may be out of service at the same time (``group_reboot_tolerance``).
"""

import threading
import time

from .executor import BackendError

OPEN, RESTORE = "asymmetry open", "asymmetry restore"
LB_DRAIN, LB_RESTORE = "asymmetry lb drain", "asymmetry lb restore"      # the load-balancer drain of one member (see drain / restore_lb)
# The three knobs. The first is the one in force now (an operational command: lost at the next commit or reboot); the other two are the persistent
# configuration. "not set" is a value: it means the factory default (reject non-SYN, drop asymmetric paths) and is put back by deleting the node.
TOLERANT = {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "bypass"}


def is_window_step(step):
    return step.startswith("asymmetry")


class Gate:
    """At most ``tolerance`` members of a group between their reboot and the end of their verification. A member that fails while it
    holds a slot may still be down, so the slot is never given back: the group is closed and its remaining members are not started."""

    def __init__(self, tolerance):
        self.tolerance, self._cond = max(int(tolerance), 1), threading.Condition()
        self._active, self._closed_by = {}, {}

    def enter(self, group, stop=None):
        """Wait for a free slot. Returns "" when it is held, otherwise the reason the member must not go on."""
        with self._cond:
            while True:
                if group in self._closed_by:
                    return f"{self._closed_by[group]} failed while out of service, so the rest of group {group!r} was not started"
                if stop is not None and stop.is_set():
                    return "stopped by you while waiting for the group to have a free slot"
                if self._active.get(group, 0) < self.tolerance:
                    self._active[group] = self._active.get(group, 0) + 1
                    return ""
                self._cond.wait(1.0)

    def leave(self, group):
        with self._cond:
            self._active[group] -= 1
            self._cond.notify_all()

    def close(self, group, member):
        with self._cond:
            self._closed_by.setdefault(group, member)
            self._cond.notify_all()


class Windows:
    """The asymmetry windows of one run. ``begin(unit)`` before a unit is run and ``end(unit)`` after it; the window opens on the first
    unit that touches it and closes after the last. ``close_all()`` is the safety net for a run that stops half way."""

    def __init__(self, plan, approved, backend, journal, say=print, settings=None):
        self.backend, self.journal, self.say, self.settings = backend, journal, say, settings
        self.lb_serials = [s for members in (plan.get("groups") or {}).values() for s in members if s in approved]
        self.windows, self.by_serial = [], {}
        for spec in plan.get("asymmetry") or []:
            serials = [s for s in spec["serials"] if s in approved]
            if not serials:
                continue
            units = [u for u in plan["order"] if any(s in serials for s in u)]
            window = {"name": f"{spec['kind']} {spec['name']}", "serials": serials, "left": len(units), "state": "new", "reason": "",
                      "lock": threading.Lock(), "opened": [], "unrestored": []}
            self.windows.append(window)
            for s in serials:
                self.by_serial[s] = window
        self.unrestored = []

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
                    return f"could not open the asymmetry window for {window['name']}: {window['reason']}"
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
        for serial in self.lb_serials:                                # a member still drained (a stopped run): put its profile back too
            if self.settings is not None:
                restore_lb(serial, self.backend, self.journal, self.settings)

    # ---- one window
    def _open(self, window):
        try:
            for serial in window["serials"]:
                self._open_one(serial, window)
        except BackendError as exc:
            reason = str(exc)
            self._close(window)                                     # put back whatever was already changed
            return "failed", reason
        return "open", ""

    def _open_one(self, serial, window):
        left = self._still_open(serial)
        if left is not None:                                        # an earlier run opened it and never restored it: keep its record, do not take a new one
            window["opened"].append((serial, left))
            self.say(f"{serial}: the asymmetry window was already open from an earlier run; keeping the values recorded then")
            return
        pre = self.backend.preflight(serial)
        if pre.get("pending_changes"):
            raise BackendError(f"{serial}: it has uncommitted candidate configuration, and the commit that sets asymmetric-path would commit it too; "
                               f"commit or discard that first")
        original = self.backend.tcp_asymmetry(serial)
        self.journal.record(serial, OPEN, "started", {"original": original, "setting": TOLERANT, "window": window["name"]})
        window["opened"].append((serial, original))                 # from here on a restore is attempted, even if the change below fails half way
        try:
            self.backend.set_tcp_asymmetry(serial, **TOLERANT)
            now = self.backend.tcp_asymmetry(serial)
            if now != TOLERANT:
                raise BackendError(f"{serial}: after the change the firewall reads {now}, not {TOLERANT}")
        except BackendError as exc:
            self.journal.record(serial, OPEN, "failed", {"original": original, "error": str(exc)})
            raise
        self.journal.record(serial, OPEN, "ok", {"original": original, "now": now, "window": window["name"]})

    def _close(self, window):
        opened, window["opened"] = window["opened"], []
        for serial, original in opened:
            self.journal.record(serial, RESTORE, "started", {"original": original})
            try:
                self.backend.set_tcp_asymmetry(serial, **original)
                now = self.backend.tcp_asymmetry(serial)
                if now != original:
                    raise BackendError(f"{serial}: after the restore the firewall reads {now}, not {original}")
            except BackendError as exc:
                self.journal.record(serial, RESTORE, "failed", {"original": original, "error": str(exc)})
                entry = {"serial": serial, "window": window["name"], "original": original, "error": str(exc)}
                window["unrestored"].append(entry)
                self.unrestored.append(entry)
                self.say(f"ALERT {serial}: could not put the TCP settings back to {original}: {exc}. Fix it by hand.")
                continue
            self.journal.record(serial, RESTORE, "ok", {"original": original, "now": now})

    def _still_open(self, serial):
        """The recorded original values when the last asymmetry record of this firewall in the journal is an open that was never restored."""
        last = None
        for event in self.journal.steps_of(serial):
            if event["step"] in (OPEN, RESTORE) and event["state"] == "ok":
                last = event
        if last is not None and last["step"] == OPEN:
            return last["detail"]["original"]
        return None


def unrestored_in_journal(journal, serials):
    """Firewalls whose journal shows the window opened (or a restore attempted) and no successful restore after it: where a person must look."""
    found = []
    for serial in serials:
        state = None
        for event in journal.steps_of(serial):
            if event["step"] == OPEN and event["state"] in ("started", "ok"):
                state = ("opened", event["detail"].get("original"))
            elif event["step"] == RESTORE and event["state"] == "ok":
                state = None
        if state:
            found.append({"serial": serial, "original": state[1]})
    return found


# ---------------------------------------------------------------- the load-balancer drain of one group member
def _protocols(settings):
    return [p for p in settings.lb_drain_probe_protocol.split("+") if p]


def drain(serial, backend, journal, settings):
    """Take the probe's protocols (ssh, http, ...) out of the interface management profile the load balancer's probe relies on, so the balancer sees the
    probe fail and drains this member gracefully, then wait lb_drain_wait_s. The original values are journaled BEFORE the change."""
    profile, protocols = settings.lb_drain_profile, _protocols(settings)
    if backend.preflight(serial).get("pending_changes"):
        raise BackendError(f"{serial}: it has uncommitted candidate configuration, and the commit that changes the profile would commit it too; "
                           f"commit or discard that first")
    found = backend.lb_profile(serial, profile, protocols)
    if not found["exists"]:
        raise BackendError(f"{serial}: the interface management profile {profile!r} is not on this firewall, so there is nothing to drain with")
    original = found["values"]
    change = {p: "no" for p in protocols if original.get(p) == "yes"}
    journal.record(serial, LB_DRAIN, "started", {"profile": profile, "original": original, "changed": sorted(change)})
    if change:
        backend.set_lb_profile(serial, profile, change)
        now = backend.lb_profile(serial, profile, protocols)["values"]
        if any(now.get(p) != "no" for p in change):
            raise BackendError(f"{serial}: after the change the profile {profile!r} reads {now}, not {sorted(change)} off")
    if settings.lb_drain_wait_s and getattr(backend, "real_time", True):
        time.sleep(settings.lb_drain_wait_s)
    journal.record(serial, LB_DRAIN, "ok", {"profile": profile, "original": original, "changed": sorted(change)})
    return {"profile": profile, "taken_out": sorted(change) or "nothing (none of them was permitted)", "waited_s": settings.lb_drain_wait_s}


def _drained(journal, serial):
    """The record of a drain that was started and never restored, else None."""
    open_ = None
    for event in journal.steps_of(serial):
        if event["step"] == LB_DRAIN and event["state"] == "started":
            open_ = event["detail"]
        elif event["step"] == LB_RESTORE and event["state"] == "ok":
            open_ = None
    return open_


def restore_lb(serial, backend, journal, settings):
    """Put the profile back as the journal recorded it. "" when there was nothing to put back or it was put back, else why it could not be."""
    opened = _drained(journal, serial)
    if not opened:
        return ""
    profile, original, changed = opened["profile"], opened["original"], opened["changed"]
    journal.record(serial, LB_RESTORE, "started", {"profile": profile, "original": original, "changed": changed})
    try:
        if changed:
            backend.set_lb_profile(serial, profile, {p: original[p] for p in changed})
            now = backend.lb_profile(serial, profile, changed)["values"]
            if any(now.get(p) != original[p] for p in changed):
                raise BackendError(f"{serial}: after the restore the profile {profile!r} reads {now}, not {original}")
    except BackendError as exc:
        journal.record(serial, LB_RESTORE, "failed", {"profile": profile, "original": original, "changed": changed, "error": str(exc)})
        return str(exc)
    journal.record(serial, LB_RESTORE, "ok", {"profile": profile, "original": original, "changed": changed})
    return ""


def undrain(serial, backend, journal, settings):
    error = restore_lb(serial, backend, journal, settings)
    if error:
        raise BackendError(error)
    return {"profile": settings.lb_drain_profile, "restored": True}


def lb_unrestored(journal, serials):
    """Members whose drain was started and whose profile is not known to be back: where a person must look (journal-derived, so it holds for a killed run too)."""
    found = []
    for serial in serials:
        opened = _drained(journal, serial)
        if opened:
            failures = [e["detail"].get("error", "") for e in journal.steps_of(serial) if e["step"] == LB_RESTORE and e["state"] == "failed"]
            found.append({"serial": serial, "profile": opened["profile"], "original": opened["original"], "changed": opened["changed"],
                          "error": failures[-1] if failures else "the restore was not attempted or did not finish"})
    return found
