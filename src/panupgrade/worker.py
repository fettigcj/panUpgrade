"""A run that does not depend on the window it was started from.

A real upgrade is carried out by ONE detached worker process: the same ``panupgrade upgrade`` command, started in its own session so a closed
terminal, a dropped SSH connection or a ``logout`` cannot reach it. What the person sees is a viewer that reads the files the worker leaves
beside the journal; the viewer can be closed and opened again any number of times.

The files, all next to the journal (``J`` is the journal's path):

    <dir>/worker.lock      who is running here (pid, journal, start time). One worker per folder of journals: a second start is refused
    J.out                  everything the worker prints, as it prints it
    J.status.json          the worker's own account of itself, rewritten every couple of seconds (state, last heartbeat, exit code)
    J.control.json         what a viewer asks of the worker: {"stop": true}
    J.viewer               the time a viewer last looked (a worker set to ``unattended = pause`` stops starting new work when nobody has)

There is no resuming of a worker that died: the next run starts from ``plan``, which reads the firewalls as they are. What a dead worker leaves
is reported (``dead_run``) so a person knows which steps were in doubt.
"""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


class WorkerBusy(Exception):
    pass


LOCK_NAME = "worker.lock"
HEARTBEAT_S = 2.0
CHECKIN_S = 5.0


# ---- small file helpers

def lock_file(directory):
    return Path(directory) / LOCK_NAME


def sidecar(journal, suffix):
    return Path(str(journal) + suffix)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Write so a reader never sees half a file."""
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---- is a process alive

def pid_alive(pid):
    """Whether a process with this id is running. A zombie (finished, not yet collected) is not."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION; os.kill must NOT be used here: it terminates
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259          # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


# ---- the lock

def active(directory):
    """The lock of a worker that is running in this folder, or None."""
    info = read_json(lock_file(directory))
    return info if info and pid_alive(info.get("pid")) else None


def left_behind(directory):
    """The lock of a worker that is NOT running any more (it was killed, or the machine went down), or None."""
    info = read_json(lock_file(directory))
    return info if info and not pid_alive(info.get("pid")) else None


def acquire(directory, info):
    """Take the lock for this process, or raise WorkerBusy naming the run already going. A lock whose process has gone is cleared."""
    os.makedirs(directory, exist_ok=True)
    path = lock_file(directory)
    for _ in range(3):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current = read_json(path)
            if current and pid_alive(current.get("pid")):
                raise WorkerBusy(f"a run is already in progress here: journal {current.get('journal')}, started {current.get('started')}, "
                                 f"process {current.get('pid')}. Watch it with `panupgrade watch`, or stop it with `panupgrade stop`.") from None
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started": now_iso(), **info}, handle, indent=1, sort_keys=True)
        return
    raise WorkerBusy(f"could not take the lock {path}")


def release(directory):
    path = lock_file(directory)
    current = read_json(path)
    if current and current.get("pid") == os.getpid():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ---- starting a worker (the process itself is started by panupgrade_live.launch: this package starts no process)

def child_env():
    """The environment for the worker: this one, plus the folder the tool is imported from, so ``-m panupgrade`` finds the same code."""
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


# ---- the worker's side

def install_signals(stop, say):
    """A worker ignores a hangup. A termination request (kill, shutdown) asks it to start nothing new and finish the steps going."""
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def asked(number, frame):
        if not stop.is_set():
            say(f"worker: signal {number} received: starting nothing new; the steps going will finish")
        stop.set()
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), asked)


class Heartbeat(threading.Thread):
    """Every couple of seconds: write the status, read the control file, and (``unattended = pause``) notice that nobody has looked for too long."""

    def __init__(self, journal, stop, mode="continue", pause_after_s=900, say=print, clock=time.time, interval=HEARTBEAT_S, verb="upgrade"):
        super().__init__(daemon=True, name="heartbeat")
        self.journal, self.stop, self.mode, self.pause_after_s, self.say, self.clock = journal, stop, mode, pause_after_s, say, clock
        self.interval, self.verb = interval, verb
        self.began, self.state, self.exit, self.reason = clock(), "running", None, ""
        self._quit = threading.Event()

    def viewer_last_seen(self):
        seen = read_json(sidecar(self.journal, ".viewer"))
        return max(self.began, float(seen["at"])) if seen and isinstance(seen.get("at"), (int, float)) else self.began

    def tick(self):
        control = read_json(sidecar(self.journal, ".control.json")) or {}
        if control.get("stop") and not self.stop.is_set():
            self.say("worker: stop requested by a viewer: starting nothing new; the steps going will finish")
            self.state, self.reason = "stopping", "stop requested"
            self.stop.set()
        elif self.mode == "pause" and not self.stop.is_set() and self.clock() - self.viewer_last_seen() > self.pause_after_s:
            minutes = round(self.pause_after_s / 60)
            self.say(f"worker: nobody has looked at this run for {minutes} minute(s) and unattended = pause: starting nothing new; "
                     f"the steps going will finish. Continue it later with --resume.")
            self.state, self.reason = "pausing", f"nobody watching for {minutes} minute(s)"
            self.stop.set()
        self.write()

    def write(self):
        write_json(sidecar(self.journal, ".status.json"),
                   {"pid": os.getpid(), "verb": self.verb, "state": self.state, "reason": self.reason, "updated": self.clock(), "updated_text": now_iso(),
                    "started": self.began, "viewer_seen": self.viewer_last_seen(), "exit": self.exit, "journal": str(self.journal)})

    def run(self):
        while not self._quit.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:                 # a heartbeat that cannot write must never stop the run
                try:
                    self.say(f"worker: heartbeat could not write its status: {exc}")
                except Exception:
                    pass

    def finish(self, code):
        """Called when the run is over, whatever way it ended: the last status says so."""
        self._quit.set()
        self.exit = code
        self.state = "paused" if self.state == "pausing" else ("stopped" if self.state == "stopping" else "finished")
        self.write()


# ---- the viewer's side

def checkin(journal, clock=time.time):
    write_json(sidecar(journal, ".viewer"), {"at": clock(), "pid": os.getpid()})


def request_stop(journal):
    write_json(sidecar(journal, ".control.json"), {"stop": True, "at": time.time(), "by_pid": os.getpid()})


def status(journal):
    return read_json(sidecar(journal, ".status.json"))


class Tail:
    """New complete lines of a growing file since the last call."""

    def __init__(self, path, start=0):
        self.path, self.offset, self.partial = Path(path), start, b""

    @classmethod
    def at_end(cls, path):
        try:
            return cls(path, Path(path).stat().st_size)
        except OSError:
            return cls(path, 0)

    def lines(self):
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return []
        self.offset += len(data)
        data = self.partial + data
        *complete, self.partial = data.split(b"\n")
        return [line.decode("utf-8", "replace").rstrip("\r") for line in complete]


class Ended(Exception):
    pass


def follow(journal, proc=None, on_line=None, on_record=None, want_stop=None, clock=time.time, sleep=time.sleep, interval=1.0,
           out_from=None, pid=None):
    """Watch a worker until it ends: pass on what it prints (``on_line``) and what it journals (``on_record``), say we are looking (check-in),
    and carry a stop request to it (``want_stop()`` true once). ``proc`` is the Popen of a worker this process started (so an ended worker is noticed
    even when it is only a zombie); otherwise the pid is taken from the status file. Returns the worker's exit code (None if it left none).
    Raising (Ctrl-C) leaves the worker running."""
    out = Tail(sidecar(journal, ".out"), out_from or 0)          # out_from: where this run's output begins in the file (a resumed run appends to it)
    records = Tail(journal, 0)
    last_checkin, asked_stop = 0.0, False

    def drain():
        for line in out.lines():
            if on_line:
                on_line(line)
        for line in records.lines():
            if on_record:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                on_record(record)

    while True:
        now = clock()
        if now - last_checkin >= CHECKIN_S:
            checkin(journal, clock)
            last_checkin = now
        if want_stop is not None and not asked_stop and want_stop():
            request_stop(journal)
            asked_stop = True
        drain()
        if proc is not None:
            ended = proc.poll() is not None
        else:
            found = status(journal) or {}
            ended = not pid_alive(pid or found.get("pid")) or found.get("state") in ("finished", "stopped", "paused")
        if ended:
            sleep(0.2)
            drain()
            found = status(journal) or {}
            return found.get("exit")                     # None: it ended without saying how (killed, or the machine went down)
        sleep(interval)


# ---- what a dead worker left

def in_doubt(journal_path):
    """The steps a journal shows as started and not finished: [(serial, step)]."""
    last, order = {}, []
    try:
        with open(journal_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "step" or record.get("state") not in ("started", "ok", "failed"):
                    continue
                key = (record.get("serial"), record.get("step"))
                if key not in last:
                    order.append(key)
                last[key] = record["state"]
    except OSError:
        return []
    return [key for key in order if last[key] == "started"]


def dead_run(directory):
    """If a worker died without finishing (its lock is still there, its process is not): what it left. None if nothing like that."""
    info = left_behind(directory)
    if not info:
        return None
    journal = info.get("journal")
    return {"journal": journal, "started": info.get("started"), "pid": info.get("pid"), "in_doubt": in_doubt(journal) if journal else [],
            "status": status(journal) if journal else None}


def python_command():
    return [sys.executable, "-m", "panupgrade"]
