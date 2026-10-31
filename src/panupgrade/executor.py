# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Carry out an approved plan against a Backend (today: only the simulator).

Rules the code enforces, each with a test:
- nothing runs without an Approval that matches the plan, names the devices, and acknowledges every caution;
- every step is journaled before and after; a step that starts is never silently dropped;
- no exception from a backend is swallowed: it fails the step, the device, and counts toward the stop limit;
- install and reboot refuse to run unless this device's backup step is verified in the journal;
- the version a device is judged on is read from that device after its own reboot;
- an HA pair is one unit, passive first; if the first member fails the second is held;
- after max_failures failed devices nothing new starts;
- a resumed run skips finished steps, never replays an in-doubt step, and never retries a failed device.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor


class BackendError(Exception):
    """A device operation failed. Always surfaced; never retried silently."""


class StepFailed(Exception):
    """The executor's own judgment that a step's outcome is not acceptable."""


class ExecutionRefused(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


class Backend:
    """What the executor may ask of a device. The simulator implements it; a live adapter comes later."""

    def preflight(self, serial): raise NotImplementedError          # {version, pending_changes, running_jobs}
    def backup(self, serial): raise NotImplementedError             # {sha256, bytes, parsed_ok}
    def download(self, serial, version): raise NotImplementedError
    def install(self, serial, version): raise NotImplementedError
    def reboot_and_wait(self, serial, timeout_s): raise NotImplementedError   # the version it came back on
    def state(self, serial): raise NotImplementedError              # {version, ha_state, running_jobs, ha_config_sync, ha_session_sync, session_count}
    def content_versions(self, serial): raise NotImplementedError   # {"app", "threat", "av", "wildfire", "url"} as installed now
    def refresh_updates(self, serial, families): raise NotImplementedError   # "check now" for content | antivirus | wildfire (read-only)
    def update_content(self, serial, family): raise NotImplementedError   # install the latest of: content | antivirus | wildfire
    def sync_to_peer(self, serial): raise NotImplementedError       # run on the ACTIVE member; returns when the sync job ends
    def suspend(self, serial): raise NotImplementedError
    def functional(self, serial): raise NotImplementedError


def _band_ok(before, after, band):
    return before > 0 and abs(after - before) / before <= band


def run(plan, approval, backend, journal, settings, stop=None):
    """Carry out the plan. Units (an HA pair, or a standalone firewall) run in parallel, up to ``settings.max_parallel``;
    the members of a pair always run in series, passive first. Ctrl-C stops new steps and lets the ones in flight finish."""
    problems = approval.problems(plan, settings.approval_max_age_hours)
    if journal.plan_hash != plan["hash"]:
        problems.append("the journal belongs to a different plan")
    if problems:
        raise ExecutionRefused(problems)
    devices = {d["serial"]: d for d in plan["devices"]}
    stop = stop or threading.Event()
    lock = threading.Lock()
    state = {"failures": sum(1 for s in devices if _failed_in_journal(journal, s))}

    def do_unit(unit):
        blocker, local = None, {}
        for serial in unit:
            started = bool(journal.steps_of(serial))
            with lock:
                over = state["failures"] >= settings.max_failures
                failures = state["failures"]
            if serial not in approval.serials:
                local[serial] = ("not_approved", "not named in the approval")
            elif blocker:
                local[serial] = ("held", f"{blocker} did not finish, so this member was not started")
            elif stop.is_set() and not started:
                local[serial] = ("not_started", "stopped by you before it started")
            elif over and not started:
                local[serial] = ("not_started", f"stopped after {failures} failed device(s)")
            else:
                was_failed = _failed_in_journal(journal, serial)
                result = _run_device(devices[serial], backend, journal, settings, stop)
                local[serial] = result
                with lock:
                    if result[0] == "failed" and not was_failed:
                        state["failures"] += 1
            if local[serial][0] in ("failed", "needs_attention", "stopped"):
                blocker = blocker or serial
        return local

    units = [unit for batch in plan["order"] for unit in batch]
    outcome = {}
    pool = ThreadPoolExecutor(max_workers=max(1, settings.max_parallel), thread_name_prefix="unit")
    futures = [pool.submit(do_unit, unit) for unit in units]
    try:
        for future in futures:
            outcome.update(future.result())
    except BaseException:           # Ctrl-C or a bug: no new steps; steps already running are allowed to finish
        stop.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True)
        raise
    pool.shutdown()
    return {"outcome": outcome, "failures": state["failures"], "stopped": state["failures"] >= settings.max_failures}


def _failed_in_journal(journal, serial):
    return any(e["state"] == "failed" for e in journal.steps_of(serial))


def _key(index, action):
    return f"{index}:{action}"


def _run_device(dev, backend, journal, settings, stop=None):
    serial = dev["serial"]
    if _failed_in_journal(journal, serial):
        return "failed", "failed earlier in this journal; not retried automatically"
    pre = journal.last_state(serial, "preflight")
    if pre == "started":
        return "needs_attention", "preflight was started and not finished; check the device before resuming"
    if pre != "ok":
        err = _step(journal, serial, "preflight", lambda: _preflight(dev, backend, settings))
        if err:
            return "failed", f"preflight: {err}"
    for index, st in enumerate(dev["steps"]):
        key = _key(index, st["action"])
        last = journal.last_state(serial, key)
        if last == "ok":
            continue
        if stop is not None and stop.is_set():
            return "stopped", f"stopped by you before {st['action']}"
        if last == "started":
            return "needs_attention", f"{st['action']} was started and not finished; check the device before resuming"
        err = _step(journal, serial, key, lambda: _do(st["action"], index, dev, backend, journal, settings))
        if err:
            return "failed", f"{st['action']}: {err}"
    return "done", ""


def _step(journal, serial, key, fn):
    journal.record(serial, key, "started")
    try:
        detail = fn() or {}
    except Exception as exc:  # deliberately broad: nothing a device does may pass unrecorded
        journal.record(serial, key, "failed", {"error": str(exc), "type": type(exc).__name__})
        return str(exc)
    journal.record(serial, key, "ok", detail)
    return ""


def _preflight(dev, backend, settings):
    seen = backend.preflight(dev["serial"])
    if seen.get("version") != dev["current_version"]:
        raise StepFailed(f"plan is stale: device reports {seen.get('version')}, plan assumed {dev['current_version']}")
    if seen.get("pending_changes") is not False and settings.pending_changes == "block":
        raise StepFailed("pending (uncommitted) configuration, or unknown; settings say block")
    if seen.get("running_jobs") != 0:
        raise StepFailed(f"jobs running or unknown: {seen.get('running_jobs')!r}")
    return {"version": seen["version"]}


def _backup_verified(journal, serial, steps):
    return any(st["action"] == "backup" and journal.last_state(serial, _key(i, "backup")) == "ok"
               for i, st in enumerate(steps))


def _expected_version(steps, index):
    for st in reversed(steps[:index]):
        m = re.fullmatch(r"install (\S+)", st["action"])
        if m:
            return m.group(1)
    return None


def _baseline(journal, dev):
    """The pair's session baseline: the count on the node that was active before the unit started."""
    for serial in (dev["serial"], dev["peer_serial"]):
        found = journal.detail(serial, "baseline") if serial else {}
        if found:
            return found["sessions"]
    return None


def _do(action, index, dev, backend, journal, settings):
    serial, steps = dev["serial"], dev["steps"]
    if action.startswith(("install ", "reboot")) and not _backup_verified(journal, serial, steps):
        raise StepFailed("no verified backup for this device in the journal; refusing to change it")
    if action.startswith("download "):
        backend.download(serial, action.split(" ", 1)[1])
    elif action == "backup":
        res = backend.backup(serial)
        if not res.get("parsed_ok") or not res.get("sha256"):
            raise StepFailed(f"backup was not verified: {res!r}")
        if _baseline(journal, dev) is None:
            source = serial if dev["ha_state"] != "passive" else dev["peer_serial"]
            journal.record(serial, "baseline", "ok", {"sessions": backend.state(source).get("session_count"), "taken_from": source})
        return {"sha256": res["sha256"], "bytes": res.get("bytes")}
    elif action == "sync to peer":
        active = serial if dev["ha_state"] == "active" else dev["peer_serial"]
        passive = dev["peer_serial"] if active == serial else serial
        if backend.state(active)["ha_state"] != "active":
            raise StepFailed(f"{active} is not the active member any more; refusing to sync from it")
        backend.sync_to_peer(active)
        after = backend.state(passive)
        if after["ha_config_sync"] is not True:
            raise StepFailed(f"after the sync, HA1 config sync reads {after['ha_config_sync']!r}, not synchronized")
        return {"synced_from": active, "to": passive}
    elif action == "fail over":
        now = backend.state(serial)
        if now["ha_state"] != "active":
            return {"skipped": f"member is {now['ha_state']} now, so there is no active role to hand over"}
        before = now["session_count"]
        backend.suspend(serial)
        after = backend.state(dev["peer_serial"])["session_count"]
        if before == 0:      # the plan said so and the operator acknowledged "no_session_baseline": nothing to compare with
            return {"sessions_before": 0, "sessions_after": after, "skipped": "no session baseline (acknowledged)"}
        if not _band_ok(before or 0, after or 0, settings.failover_band):
            backend.functional(serial)
            raise StepFailed(f"after failover the new active node has {after} sessions vs {before} before, "
                             f"outside {int(settings.failover_band * 100)}%; reverted")
        return {"sessions_before": before, "sessions_after": after}
    elif action == "hold suspended":
        if backend.state(serial)["ha_state"] != "suspended":
            backend.suspend(serial)
    elif action.startswith("install "):
        backend.install(serial, action.split(" ", 1)[1])
    elif action == "reboot":
        return {"version": backend.reboot_and_wait(serial, settings.reboot_timeout_s)}
    elif action == "verify":
        want, got = _expected_version(steps, index), backend.state(serial)
        if got["version"] != want:
            raise StepFailed(f"device reports {got['version']}, expected {want}")
        if got["running_jobs"] != 0:
            raise StepFailed(f"jobs running or unknown: {got['running_jobs']!r}")
        return {"version": got["version"]}
    elif action == "go live":
        backend.functional(serial)
    elif action == "verify HA":
        got = backend.state(serial)
        accepted_unsynced = any((r.get("evidence") or {}).get("config_unsynced") for r in dev["results"])
        if got["ha_config_sync"] is not True and not accepted_unsynced:
            raise StepFailed(f"HA1 config sync is {got['ha_config_sync']!r}")
        if got["ha_session_sync"] is not True:
            raise StepFailed(f"HA2 session sync is {got['ha_session_sync']!r}")
        if got["ha_state"] == "active":
            base = _baseline(journal, dev)
            if base == 0:
                return {"ha_state": got["ha_state"], "skipped": "no session baseline (acknowledged)"}
            if not base or not _band_ok(base, got["session_count"] or 0, settings.post_upgrade_band):
                raise StepFailed(f"active node has {got['session_count']} sessions vs baseline {base}, "
                                 f"outside {int(settings.post_upgrade_band * 100)}% or no baseline")
        return {"ha_state": got["ha_state"]}
    elif action == "restore roles":
        peer = dev["peer_serial"]
        backend.suspend(peer)
        backend.functional(peer)
    else:
        raise StepFailed(f"unknown step {action!r}")
    return {}
