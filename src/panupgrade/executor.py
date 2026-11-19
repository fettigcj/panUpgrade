# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Carry out an approved plan against a Backend (today: only the simulator).

Rules the code enforces, each with a test:
- nothing runs without an Approval that matches the plan, names the devices, and acknowledges every caution;
- every step is journaled before and after; a step that starts is never silently dropped;
- no exception from a backend is swallowed: it fails the step, the device, and counts toward the stop limit;
- install and reboot refuse to run unless this device's backup step is verified in the journal;
- the version a device is judged on is read from that device after its own reboot;
- an HA pair is one unit, passive first (or active first, setting upgrade_first); if the first member fails the second is held;
- after max_failures failed devices nothing new starts;
- a resumed run skips finished steps, never replays an in-doubt step, and never retries a failed device.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import progress


class BackendError(Exception):
    """A device operation failed. Always surfaced; never retried silently."""


class CommitRefused(BackendError):
    """The firewall answered a commit with an error, so nothing was committed (the candidate edits made for it were taken back)."""


class StepFailed(Exception):
    """The executor's own judgment that a step's outcome is not acceptable."""


class ExecutionRefused(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


class Backend:
    """What the executor may ask of a device. The simulator implements it; a live adapter comes later."""

    real_time = True      # False for a simulation: waits that only exist because real firewalls take time are skipped

    def running_jobs(self, serial):                                 # how many jobs are not finished; one cheap look where the backend can
        return self.state(serial).get("running_jobs")

    def preflight(self, serial): raise NotImplementedError          # {version, pending_changes, running_jobs}
    def backup(self, serial): raise NotImplementedError             # {sha256, bytes, parsed_ok}
    def download(self, serial, version): raise NotImplementedError
    def delete_image(self, serial, version): raise NotImplementedError   # remove a downloaded PAN-OS image; never the running one
    def install(self, serial, version): raise NotImplementedError
    def reboot_and_wait(self, serial, timeout_s): raise NotImplementedError   # the version it came back on
    def state(self, serial): raise NotImplementedError              # {version, ha_state, running_jobs, ha_config_sync, ha_session_sync, session_count}
    def content_versions(self, serial): raise NotImplementedError   # {"app", "threat", "av", "wildfire", "url"} as installed now
    def refresh_updates(self, serial, families): raise NotImplementedError   # "check now" for content | antivirus | wildfire (read-only)
    def update_content(self, serial, family): raise NotImplementedError   # install the latest of: content | antivirus | wildfire
    def sync_to_peer(self, serial): raise NotImplementedError       # run on the ACTIVE member; returns when the sync job ends
    def suspend(self, serial): raise NotImplementedError
    def functional(self, serial): raise NotImplementedError
    def lb_profile(self, serial, profile, protocols): raise NotImplementedError    # {"exists": bool, "values": {protocol: "yes" | "no" | None (not set)}, "origin": "local" | "template" | None} of an interface management profile
    def set_lb_profile(self, serial, profile, changes): raise NotImplementedError   # {protocol: "yes" | "no" | None (delete the node)}; one commit on the firewall. A template-held profile gets a same-named local copy
    def remove_lb_override(self, serial, profile): raise NotImplementedError        # delete that local copy (one commit): the template's profile resumes
    def tcp_asymmetry(self, serial): raise NotImplementedError      # {"reject_non_syn": bool (in force now), "reject_non_syn_config": "yes" | "no" | None (not set), "asymmetric_path": "drop" | "bypass" | None (not set)}
    def set_tcp_asymmetry(self, serial, reject_non_syn, reject_non_syn_config, asymmetric_path): raise NotImplementedError   # the two configuration values (None deletes the node), one commit on the firewall, then the in-force flag


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
    from .windows import Gate, Windows, lb_unrestored
    devices = {d["serial"]: d for d in plan["devices"]}
    stop = stop or threading.Event()
    gate = Gate(settings.group_reboot_tolerance)
    windows = Windows(plan, approval.serials, backend, journal, say=progress.say, settings=settings)
    lock = threading.Lock()
    state = {"failures": sum(1 for s in devices if _failed_in_journal(journal, s))}

    def do_unit(unit):
        refusal = windows.begin([s for s in unit if s in approval.serials])
        if refusal:
            return {serial: ("held", refusal) for serial in unit}
        try:
            return run_unit(unit)
        finally:
            windows.end([s for s in unit if s in approval.serials])

    def run_unit(unit):
        blocker, local = None, {}
        for serial in unit:
            started = any(not e["step"].startswith("asymmetry") for e in journal.steps_of(serial))
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
                result = _run_device(devices[serial], backend, journal, settings, stop, gate)
                local[serial] = result
                with lock:
                    if result[0] == "failed" and not was_failed:
                        state["failures"] += 1
            if local[serial][0] in ("failed", "needs_attention", "stopped"):
                blocker = blocker or serial
        return local

    units = plan["order"]
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
        windows.close_all()         # whatever happened, a window that was opened is closed again
        raise
    pool.shutdown()
    windows.close_all()
    upgraded = [serial for serial, dev in devices.items() if dev["steps"] and _standing(dev, journal, len(dev["steps"])).startswith("already on")]
    return {"outcome": outcome, "failures": state["failures"], "stopped": state["failures"] >= settings.max_failures, "upgraded": upgraded,
            "asymmetry_unrestored": windows.unrestored, "lb_unrestored": lb_unrestored(journal, [s for unit in units for s in unit])}


def _failed_in_journal(journal, serial):
    return any(e["state"] == "failed" and not e["step"].startswith("asymmetry") for e in journal.steps_of(serial))


def _key(index, action):
    return f"{index}:{action}"


def _run_device(dev, backend, journal, settings, stop=None, gate=None):
    """Run one firewall's steps. A member of a stand-alone group takes a slot of its group from its reboot to the end of its verification;
    leaving this function while still holding the slot (a failure, a stop) closes the group, because that firewall may still be down."""
    group = dev.get("group") if gate is not None else ""
    held, result = [], None
    try:
        result = _run_steps(dev, backend, journal, settings, stop, gate, group, held)
        return result
    finally:
        if result is None or result[0] in ("failed", "stopped", "held"):      # undo what was edited for the load balancer, as far as it can be undone
            from .windows import restore_lb
            restore_lb(dev["serial"], backend, journal, settings)
        if held:
            gate.close(group, dev["hostname"])


def _run_steps(dev, backend, journal, settings, stop, gate, group, held):
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
        if group and (st["action"] in ("drain", "reboot", "verify") or st["action"].startswith("reboot to activate ")) and not held:
            why = gate.enter(group, stop)
            if why:
                return "held", why
            held.append(group)
            if not journal.detail(serial, "group_baseline"):                     # the count the member is judged against when it comes back
                journal.record(serial, "group_baseline", "ok", {"sessions": _sessions(backend, serial)})
        err = _step(journal, serial, key, lambda: _do(st["action"], index, dev, backend, journal, settings))
        if err:
            return "failed", f"{_standing(dev, journal, index)}; {st['action']} failed: {err}"
        # A group member keeps its slot until its LAST step that matters: the service check when there is one (the next member is not drained
        # until this one carries traffic again), else the undrain, else its last verify. A path of several installs, or an activation reboot
        # followed by an install, has several verifies; the first of them is not the end.
        actions = [x["action"] for x in dev["steps"]]
        last_verify = max((i for i, x in enumerate(actions) if x == "verify"), default=-1)
        final = ("check service" if "check service" in actions else "undrain" if "undrain" in actions else None)
        if held and ((final and st["action"] == final) or (not final and index == last_verify)):
            held.clear()
            gate.leave(group)
    return "done", ""


def _standing(dev, journal, index):
    """Where a firewall stands when the step at ``index`` did not finish: which version it is on matters more than which step it was."""
    serial, steps = dev["serial"], dev["steps"]

    def ok(i):
        return journal.last_state(serial, _key(i, steps[i]["action"])) == "ok"
    installs = [i for i in range(index) if steps[i]["action"].startswith("install ") and ok(i)]
    reboots = [i for i in range(index) if steps[i]["action"] == "reboot" and ok(i)]
    if index < len(steps) and steps[index]["action"].startswith("reboot to activate "):
        return f"still on {dev['current_version']}; {steps[index]['action'].split(' ', 3)[3]} is installed and has not been booted"
    if installs and reboots and reboots[-1] > installs[-1]:
        return f"already on {steps[installs[-1]]['action'].split(' ', 1)[1]}"
    if installs and index < len(steps) and steps[index]["action"] == "reboot":
        return "the new image is installed and the reboot did not finish as far as the tool could see (the firewall may well have come back since: check it)"
    if installs:
        return "the new image is installed but it has not rebooted into it"
    return f"not upgraded (still on {dev['current_version']})"


# What a firewall says when it is busy with something that will finish by itself (a commit, an auto-commit, a queued job).
BUSY = re.compile(r"commit (is )?in progress|try again later|commit task queue|pending jobs|job .{0,30}in progress", re.I)


def _jobs_now(backend, serial):
    """How many jobs the firewall has not finished, by the cheapest look the backend has (a stub with only ``state`` works too)."""
    look = getattr(backend, "running_jobs", None)
    return look(serial) if look else backend.state(serial).get("running_jobs")


def _settle(journal, backend, serial, key, settings, seconds, what):
    """Wait ``seconds``, then until the firewall has no unfinished job, before the next step touches it.

    From the 2026-11-03 lab run: the firewall starts a full auto-commit of its own at moments the tool causes but cannot see coming (18-24 s after
    each of four suspends; a second one 4-52 s after a rebooted member reconnects to Panorama), and one lasts 63-143 s. A "no jobs" look in the
    gap before it starts proves nothing, so the window is waited out first and only a quiet list after it counts. If nothing starts, the
    wait is just the window. ``seconds`` 0 turns the window off (the list must still be clear). A simulation does not wait."""
    if seconds <= 0 or not getattr(backend, "real_time", True):
        return {}
    start, noted, step = time.monotonic(), False, min(settings.poll_interval_s, 5)
    while True:
        jobs, elapsed = _jobs_now(backend, serial), time.monotonic() - start
        if jobs and not noted:
            noted = True
            journal.record(serial, key, "note", {"message": f"{jobs} job(s) running {what} (a commit the firewall started itself): waiting for it to finish"})
        if elapsed >= seconds and jobs == 0:
            return {"settled_s": round(elapsed), "job_seen": noted}
        if elapsed >= seconds + settings.busy_wait_s:
            raise StepFailed(f"{serial} still has {jobs!r} unfinished job(s) {round(elapsed)}s {what}, so the next step was not started")
        time.sleep(step)


def _wait_for_quiet(backend, serial, settings, what, timeout_s=None):
    """A config sync, an HA state change or a boot queues a commit on the firewall, and the install is refused while that is still in the
    queue ("pending jobs in the commit task queue"). Wait for ``show jobs all`` to show nothing unfinished, rather than fail a firewall
    that is merely busy. The wait is ``busy_wait_s`` unless a caller (the config sync) asks for ``sync_timeout_s``."""
    limit = settings.busy_wait_s if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit
    while True:
        jobs = _jobs_now(backend, serial)
        if jobs == 0:
            return
        if time.monotonic() >= deadline:
            raise StepFailed(f"{serial} still has {jobs!r} job(s) in its queue after {limit}s, so {what} was not started")
        time.sleep(settings.poll_interval_s)


def _retry_while_busy(journal, serial, key, settings, command):
    """Run a command the firewall may refuse because it is busy. The jobs list showed nothing a moment ago, but a commit can be queued
    between that look and the command (an HA change queues one), so a refusal that says "commit in progress" / "try again later" is
    not a failure: wait ``poll_interval_s`` and ask again, up to ``busy_wait_s``. A refusal changed nothing, so asking again is safe.
    Any other refusal is raised at once."""
    deadline, tries = time.monotonic() + settings.busy_wait_s, 0
    while True:
        try:
            return command()
        except BackendError as exc:
            if not BUSY.search(str(exc)) or time.monotonic() >= deadline:
                raise
            tries += 1
            journal.record(serial, key, "note", {"message": f"the firewall is busy ({str(exc).rsplit(': ', 1)[-1].strip()}); asking again "
                                                            f"in {settings.poll_interval_s}s (attempt {tries + 1})"})
            time.sleep(settings.poll_interval_s)


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
    if seen.get("pending_reboot") is True and not (dev.get("activate") and seen.get("pending_reboot_version") == dev["activate"]["version"]):
        raise StepFailed(f"{seen.get('pending_reboot_version') or 'an image'} is installed and waiting for a reboot, and the firewall refuses to commit "
                         f"until it has been rebooted; reboot it first")
    return {"version": seen["version"]}


def _backup_verified(journal, serial, steps):
    return any(st["action"] == "backup" and journal.last_state(serial, _key(i, "backup")) == "ok"
               for i, st in enumerate(steps))


def _target_version(steps):
    """The version a device ends on: the last install in its steps."""
    return _expected_version(steps, len(steps))


def _expected_version(steps, index):
    for st in reversed(steps[:index]):
        m = re.fullmatch(r"(?:install|reboot to activate) (\S+)", st["action"])
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


def _sessions(backend, serial):
    try:
        return backend.state(serial).get("session_count") or 0
    except Exception:           # a member that cannot be read has no count to compare with; that is not this member's failure
        return 0


def _check_service(dev, backend, journal, settings, key):
    """A group member is back (upgraded, undrained): wait until its session table is within ``group_session_warn_band`` of what it was before it
    was drained, or of what the other members hold now, whichever it is closer to (the load balancer hands traffic back gradually, and the members
    were never equal). Past ``group_session_wait_s`` still short: a warning, or a failure when ``group_session_fail_band`` or more short. Only a
    shortfall counts; a member that holds more is not a problem. A simulation does not wait: it is judged on one reading."""
    serial = dev["serial"]
    own = (journal.detail(serial, "group_baseline") or {}).get("sessions") or 0
    warn, fail = settings.group_session_warn_band, settings.group_session_fail_band
    start, noted, real = time.monotonic(), False, getattr(backend, "real_time", True)
    while True:
        mine = _sessions(backend, serial)
        others = [n for n in (_sessions(backend, peer) for peer in dev.get("group_peers") or []) if n]
        average = sum(others) / len(others) if others else 0
        references = [r for r in (own, average) if r > 0]
        elapsed = time.monotonic() - start
        if not references:
            return {"skipped": "no session count to compare with", "sessions_after": mine}
        best = max(mine / r for r in references)
        detail = {"sessions_before": own, "sessions_after": mine, "others_average": round(average), "waited_s": round(elapsed)}
        if best >= 1 - warn:
            return detail
        if not real or elapsed >= settings.group_session_wait_s:
            break
        if not noted:
            noted = True
            journal.record(serial, key, "note", {"message": f"{mine} sessions, {round((1 - best) * 100)}% short of its normal count: waiting up to "
                                                            f"{settings.group_session_wait_s}s for it to come back before the next member is touched"})
        time.sleep(settings.poll_interval_s)
    words = (f"{mine} sessions against {own} before the drain and {round(average)} on the other members: {round((1 - best) * 100)}% short "
             f"after {round(elapsed)}s")
    if 1 - best >= fail:
        raise StepFailed(f"the member is not carrying traffic again: {words} (failure at {int(fail * 100)}% or more)")
    return {**detail, "warning": f"{words}; the run goes on (warning above {int(warn * 100)}%)"}


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
            source = serial if dev["ha_state"] not in ("passive", "suspended") else dev["peer_serial"]
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
        _wait_for_quiet(backend, passive, settings, "anything further", settings.sync_timeout_s)
        return {"synced_from": active, "to": passive}
    elif action == "wait for the passive firewall's upgrade to finish" or action == "wait for the first member's upgrade to finish":
        # The unit runs the passive member first and never starts this one unless it finished. This is the checkpoint that proves it:
        # failing over to a peer that is not on the version this member is going to is refused.
        want, got = _target_version(steps), backend.state(dev["peer_serial"])["version"]
        if want and got != want:
            raise StepFailed(f"the other member {dev['peer_serial']} runs {got}, not {want}; refusing to hand over to a peer that is not upgraded")
        return {"peer_version": got}
    elif action == "fail over":
        now = backend.state(serial)
        if now["ha_state"] != "active":
            return {"skipped": f"member is {now['ha_state']} now, so there is no active role to hand over"}
        before = now["session_count"]
        backend.suspend(serial)
        after = backend.state(dev["peer_serial"])["session_count"]
        if before == 0:      # the plan said so and the operator acknowledged "no_session_baseline": nothing to compare with
            detail = {"sessions_before": 0, "sessions_after": after, "skipped": "no session baseline (acknowledged)"}
        elif not _band_ok(before or 0, after or 0, settings.failover_band):
            backend.functional(serial)
            raise StepFailed(f"after failover the new active node has {after} sessions vs {before} before, "
                             f"outside {int(settings.failover_band * 100)}%; reverted")
        else:
            detail = {"sessions_before": before, "sessions_after": after}
        # the suspended member now queues its own commit; the install must not meet it half-started
        return {**detail, **_settle(journal, backend, serial, action, settings, settings.settle_after_failover_s, "after the fail-over")}
    elif action == "hold suspended":
        if backend.state(serial)["ha_state"] != "suspended":
            backend.suspend(serial)
    elif action.startswith("install "):
        _wait_for_quiet(backend, serial, settings, "the install")
        _retry_while_busy(journal, serial, action, settings, lambda: backend.install(serial, action.split(" ", 1)[1]))
    elif action == "drain":
        from .windows import drain
        return drain(serial, backend, journal, settings)
    elif action == "undrain":
        from .windows import undrain
        return undrain(serial, backend, journal, settings)
    elif action == "check service":
        return _check_service(dev, backend, journal, settings, _key(index, action))
    elif action == "reboot" or action.startswith("reboot to activate "):
        version = backend.reboot_and_wait(serial, settings.reboot_timeout_s)
        # a rebooted member reconnects to Panorama and then runs a second auto-commit of its own; wait that out before calling it back
        return {"version": version, **_settle(journal, backend, serial, action, settings, settings.settle_after_reboot_s, "after the reboot")}
    elif action == "verify":
        _wait_for_quiet(backend, serial, settings, "the verification")      # the auto-commit after a boot may still be running
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
        if got["ha_state"] == "suspended" and dev["ha_state"] == "suspended":
            raise StepFailed("the member was suspended when planned and is still suspended after the upgrade, so the reboot did not clear the "
                             "suspension; make it functional by hand (or run the tool again with the member suspended) when you want it back in the pair")
        if got["ha_session_sync"] is not True:
            raise StepFailed(f"HA2 session sync is {got['ha_session_sync']!r}")
        # Config sync is judged BEFORE the first member is upgraded (preflight and prepare). While the peer runs another version it cannot sync
        # and nothing can be done about it, so it is not asked; once both match it is only reported, never a reason to fail a firewall.
        peer = dev.get("peer_serial")
        peer_version = backend.state(peer)["version"] if peer else None
        said = {"config_sync": got["ha_config_sync"]} if peer_version == got["version"] else {"peer_version": peer_version}
        if got["ha_state"] == "active":
            base = _baseline(journal, dev)
            if base == 0:
                return {"ha_state": got["ha_state"], "skipped": "no session baseline (acknowledged)", **said}
            if not base or not _band_ok(base, got["session_count"] or 0, settings.post_upgrade_band):
                raise StepFailed(f"active node has {got['session_count']} sessions vs baseline {base}, "
                                 f"outside {int(settings.post_upgrade_band * 100)}% or no baseline")
        elif dev.get("position") == "second" and peer:
            # the last member of a pair is done: service is where it was before? The node that carries the traffic now is the peer, so judge that one.
            theirs = backend.state(peer)
            base = _baseline(journal, dev)
            if theirs["ha_state"] == "active" and base:
                if not _band_ok(base, theirs["session_count"] or 0, settings.post_upgrade_band):
                    raise StepFailed(f"the pair is upgraded but the active node {peer} has {theirs['session_count']} sessions vs baseline {base}, "
                                     f"outside {int(settings.post_upgrade_band * 100)}%")
                said["active_sessions"] = theirs["session_count"]
        return {"ha_state": got["ha_state"], **said}
    elif action == "restore roles":
        peer = dev["peer_serial"]
        backend.suspend(peer)
        backend.functional(peer)
    else:
        raise StepFailed(f"unknown step {action!r}")
    return {}
