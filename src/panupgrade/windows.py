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

from .executor import BackendError, CommitRefused

OPEN, RESTORE = "asymmetry open", "asymmetry restore"
LB_DRAIN, LB_RESTORE = "asymmetry lb drain", "asymmetry lb restore"      # the load-balancer drain of one member (see drain / restore_lb)
# The three knobs. The first is the one in force now (an operational command: lost at the next commit or reboot); the other two are the persistent
# configuration. "not set" is a value: it means the factory default (reject non-SYN, drop asymmetric paths) and is put back by deleting the node.
TOLERANT = {"reject_non_syn": False, "reject_non_syn_config": "no", "asymmetric_path": "bypass"}


def _ledger(journal):
    return getattr(journal, "ledger", None)


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

    def closed_by(self, group):
        """The member whose failure closed this group, or ""."""
        with self._cond:
            return self._closed_by.get(group, "")

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
        # A firewall with an image waiting for its reboot refuses commits, and it is about to go down anyway: the sessions it drains land on the OTHER
        # members, which are the ones that must tolerate them. So its own window is opened after its activation reboot (see after_activation).
        self.deferred = {d["serial"] for d in plan.get("devices") or [] if d.get("activate")}
        for spec in plan.get("asymmetry") or []:
            planned = [s for s in spec["serials"] if s in approved]
            if not planned:
                continue
            serials = planned + [s for s in spec.get("support") or [] if s not in planned]        # the rest of a group takes the drained sessions too
            units = [u for u in plan["order"] if any(s in planned for s in u)]
            window = {"name": f"{spec['kind']} {spec['name']}", "serials": serials, "planned": len(planned), "left": len(units), "state": "new",
                      "reason": "", "lock": threading.Lock(), "opened": [], "unrestored": []}
            self.windows.append(window)
            for s in serials:
                self.by_serial[s] = window
        self.unrestored = []
        # The other members of a window wait (before they drain or reboot) until every member that has to reboot first has done so and its own window
        # is open: the sessions the next member drains must find tolerant firewalls, and a member that is still down cannot take them.
        self._cond = threading.Condition()
        self.awaiting = {w["name"]: {s for s in w["serials"] if s in self.deferred} for w in self.windows if len(w["serials"]) > 1}

    def wait_for_deferred(self, serial, stop=None):
        """Block while a member of this firewall's window that must reboot first has not yet done so. "" when it may go on, else why it must not."""
        window = self.by_serial.get(serial)
        if window is None or serial in self.deferred:
            return ""
        with self._cond:
            while self.awaiting.get(window["name"]):
                if stop is not None and stop.is_set():
                    return "stopped by you while waiting for the member with a pending reboot to come back"
                self._cond.wait(1.0)
        return ""

    def resolve(self, serial):
        """This firewall no longer holds up the others of its window: its window is open, or it will not be started."""
        with self._cond:
            for waiting in self.awaiting.values():
                waiting.discard(serial)
            self._cond.notify_all()

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

    def after_activation(self, serial):
        try:
            return self._open_after_activation(serial)
        finally:
            self.resolve(serial)

    def _open_after_activation(self, serial):
        """"" when nothing was needed or the window is now open on this firewall; else why it could not be opened (the firewall's step then fails)."""
        window = self.by_serial.get(serial)
        if window is None or serial not in self.deferred or window["planned"] < 2:      # the only member going down: nobody else's drain to tolerate
            return ""
        with window["lock"]:
            if window["state"] != "open" or any(s == serial for s, _o in window["opened"]):
                return ""
            try:
                self._open_one(serial, window)
            except BackendError as exc:
                return f"could not open the asymmetry window on it after its reboot: {exc}"
        return ""

    def end(self, unit):
        for serial in unit:
            self.resolve(serial)
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
                if serial not in self.deferred:
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
        clear_candidate(self.backend, self.journal, serial, self.settings, "the commit that sets asymmetric-path")
        original = self.backend.tcp_asymmetry(serial)
        self.journal.record(serial, OPEN, "started", {"original": original, "setting": TOLERANT, "window": window["name"]})
        if _ledger(self.journal):
            _ledger(self.journal).set_window(serial, original, window["name"], self.journal.path)      # remembered outside this run, before anything changes
        window["opened"].append((serial, original))                 # from here on a restore is attempted, even if the change below fails half way
        try:
            self.backend.set_tcp_asymmetry(serial, **TOLERANT)
            now = self.backend.tcp_asymmetry(serial)
            if now != TOLERANT:
                raise BackendError(f"{serial}: after the change the firewall reads {now}, not {TOLERANT}")
        except CommitRefused as exc:                                # nothing was committed and the edits were taken back: there is nothing to restore
            window["opened"].remove((serial, original))
            if _ledger(self.journal):
                _ledger(self.journal).clear_window(serial)
            self.journal.record(serial, OPEN, "failed", {"original": original, "error": str(exc), "changed": False})
            raise
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
            if _ledger(self.journal):
                _ledger(self.journal).clear_window(serial)

    def _still_open(self, serial):
        """The recorded original values when the last asymmetry record of this firewall in the journal is an open that was never restored."""
        last = None
        for event in self.journal.steps_of(serial):
            if event["step"] in (OPEN, RESTORE) and event["state"] == "ok":
                last = event
        if last is not None and last["step"] == OPEN:
            return last["detail"]["original"]
        if last is None and _ledger(self.journal):                    # an EARLIER run left it open: its values, not what the firewall reads now
            held = _ledger(self.journal).window(serial)
            if held:
                self.say(f"{serial}: the asymmetry window was left open by an earlier run ({held.get('window')}, {held.get('since')}); keeping the values recorded then")
                return held["original"]
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
            elif event["step"] == OPEN and event["state"] == "failed" and event["detail"].get("changed") is False:
                state = None                                         # the firewall refused the commit: nothing was ever changed
        if state:
            found.append({"serial": serial, "original": state[1]})
    return found


REVERT = "asymmetry revert candidate"


def clear_candidate(backend, journal, serial, settings, why):
    """A commit on the firewall commits EVERYTHING in its candidate configuration, and the tool cannot know whether what is there is legitimate. So it
    never commits it: with pending_changes=revert it discards the candidate (revert to the running configuration), journaled; otherwise it stops and says so."""
    if not backend.preflight(serial).get("pending_changes"):
        return
    if settings is None or settings.pending_changes != "revert":
        raise BackendError(f"{serial}: it has uncommitted candidate configuration, and {why} would commit it too. Commit it yourself if it is wanted, or "
                           f"set pending_changes=revert to have the tool discard it (revert to the running configuration) first")
    journal.record(serial, REVERT, "started", {"why": why})
    try:
        backend.revert_candidate(serial)
    except BackendError as exc:
        journal.record(serial, REVERT, "failed", {"error": str(exc)})
        raise
    journal.record(serial, REVERT, "ok", {"why": why, "message": "the uncommitted candidate configuration was discarded (reverted to running)"})


def restore_leftovers(entries, backend, journal, settings, say=print):
    """Put back what an earlier run left changed (the ledger's entries). Returns [(serial, what, "" or why it could not be)]. A firewall that has to
    reboot before it takes a commit is left as it is and said so: the next upgrade run restores it after its reboot."""
    found = []
    for serial, kinds in sorted(entries.items()):
        if "window" in kinds:
            original = kinds["window"]["original"]
            journal.record(serial, RESTORE, "started", {"original": original, "leftover": True})
            try:
                backend.set_tcp_asymmetry(serial, **original)
                now = backend.tcp_asymmetry(serial)
                if now != original:
                    raise BackendError(f"{serial}: after the restore the firewall reads {now}, not {original}")
            except BackendError as exc:
                journal.record(serial, RESTORE, "failed", {"original": original, "error": str(exc), "leftover": True})
                found.append((serial, "asymmetry window", str(exc)))
            else:
                journal.record(serial, RESTORE, "ok", {"original": original, "now": now, "leftover": True})
                if _ledger(journal):
                    _ledger(journal).clear_window(serial)
                found.append((serial, "asymmetry window", ""))
        if "drain" in kinds:
            error = restore_lb(serial, backend, journal, settings)
            found.append((serial, "load balancer drain", error))
    return found


# ---------------------------------------------------------------- the load-balancer drain of one group member
def _protocols(settings):
    return [p for p in settings.lb_drain_probe_protocol.split("+") if p]


def drain(serial, backend, journal, settings):
    """Take the probe's protocols (ssh, http, ...) out of the interface management profile the load balancer's probe relies on, so the balancer sees the
    probe fail and drains this member gracefully, then wait lb_drain_wait_s. The original values are journaled BEFORE the change."""
    profile, protocols = settings.lb_drain_profile, _protocols(settings)
    clear_candidate(backend, journal, serial, settings, "the commit that changes the profile")
    found = backend.lb_profile(serial, profile, protocols)
    if not found["exists"]:
        raise BackendError(f"{serial}: the interface management profile {profile!r} is not on this firewall, so there is nothing to drain with")
    original, origin = found["values"], found.get("origin") or "local"
    change = {p: "no" for p in protocols if original.get(p) == "yes"}
    journal.record(serial, LB_DRAIN, "started", {"profile": profile, "original": original, "changed": sorted(change), "origin": origin})
    if _ledger(journal):
        _ledger(journal).set_drain(serial, {"profile": profile, "original": original, "changed": sorted(change), "origin": origin}, journal.path)
    if change:
        try:
            backend.set_lb_profile(serial, profile, change)
        except PermissionError as exc:                      # refused by the tool's own allow-list before anything was sent: nothing was changed
            journal.record(serial, LB_DRAIN, "failed", {"error": str(exc), "nothing_sent": True})
            if _ledger(journal):
                _ledger(journal).clear_drain(serial)
            raise
        now = backend.lb_profile(serial, profile, protocols)["values"]
        if any(now.get(p) != "no" for p in change):
            raise BackendError(f"{serial}: after the change the profile {profile!r} reads {now}, not {sorted(change)} off")
    if settings.lb_drain_wait_s and getattr(backend, "real_time", True):
        time.sleep(settings.lb_drain_wait_s)
    journal.record(serial, LB_DRAIN, "ok", {"profile": profile, "original": original, "changed": sorted(change), "origin": origin})
    return {"profile": profile, "taken_out": sorted(change) or "nothing (none of them was permitted)", "waited_s": settings.lb_drain_wait_s}


def _drained(journal, serial):
    """The record of a drain that was started and never restored, else None."""
    open_ = None
    for event in journal.steps_of(serial):
        if event["step"] == LB_DRAIN and event["state"] == "started":
            open_ = event["detail"]
        elif event["step"] == LB_RESTORE and event["state"] == "ok":
            open_ = None
        elif event["step"] == LB_DRAIN and event["state"] == "failed" and event["detail"].get("nothing_sent"):
            open_ = None
    if open_ is None and _ledger(journal) and not any(e["step"] in (LB_DRAIN, LB_RESTORE) for e in journal.steps_of(serial)):
        open_ = _ledger(journal).drain(serial)                    # an earlier run's drain, never put back
    return open_


def restore_lb(serial, backend, journal, settings):
    """Put the profile back as the journal recorded it. "" when there was nothing to put back or it was put back, else why it could not be."""
    opened = _drained(journal, serial)
    if not opened:
        return ""
    profile, original, changed = opened["profile"], opened["original"], opened["changed"]
    from_template = opened.get("origin") == "template"
    journal.record(serial, LB_RESTORE, "started", {"profile": profile, "original": original, "changed": changed, "origin": opened.get("origin")})
    try:
        if changed:
            if from_template:          # the profile was the template's: the exact prior state is "no local copy", so delete it instead of writing values back
                backend.remove_lb_override(serial, profile)
                after = backend.lb_profile(serial, profile, changed)
                if after.get("origin") != "template":
                    raise BackendError(f"{serial}: after removing the local copy the profile {profile!r} is still held locally")
            else:
                backend.set_lb_profile(serial, profile, {p: original[p] for p in changed})
            now = backend.lb_profile(serial, profile, changed)["values"]
            if any(now.get(p) != original[p] for p in changed):
                raise BackendError(f"{serial}: after the restore the profile {profile!r} reads {now}, not {original}")
    except BackendError as exc:
        journal.record(serial, LB_RESTORE, "failed", {"profile": profile, "original": original, "changed": changed, "error": str(exc)})
        return str(exc)
    journal.record(serial, LB_RESTORE, "ok", {"profile": profile, "original": original, "changed": changed})
    if _ledger(journal):
        _ledger(journal).clear_drain(serial)
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
