# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""``panupgrade`` with no arguments: the whole workflow on one screen.

A home screen lists the steps in order (sign in, choose firewalls, read the fleet, plan, prepare, upgrade) with where each one
stands, and a few keys jump between them from anywhere: Alt-H home, Alt-A sign in, Alt-F firewalls, Alt-R report, Alt-P plan,
Alt-S settings, Alt-J jobs, or Alt-1 to Alt-9 for the steps in order. Each step is the same code the command of that name runs, so
the screen and the command line cannot disagree: long work (reading Panorama) runs in the background while the screen stays alive.

Background work is kept by the shell, not by the screen that started it. Leave the report while it is still reading and the answer is
waiting when you come back; come back later and the last answer is still there, marked with how old it is. Every piece of
background work is listed on the Jobs screen (Alt-J) and, on a wide window, in a pane down the right-hand side of every screen.

Screens are small objects with ``render(width, height)``, ``press(key)`` (returning None, 'home', 'quit' or ('goto', name)),
``enter()`` and, where they have something of their own to poll, ``tick()``. The terminal loop is in ``tui``; nothing here touches
the terminal.
"""

import io
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from . import cli, credstore, profiles, progress, site
from .sources import SourceError

SPIN = "|/-\\"
HOTKEYS = [("alt-h", "home", "Alt-H home"), ("alt-a", "auth", "Alt-A sign in"), ("alt-f", "select", "Alt-F firewalls"),
           ("alt-r", "report", "Alt-R report"), ("alt-p", "plan", "Alt-P plan"), ("alt-s", "settings", "Alt-S settings"),
           ("alt-j", "jobs", "Alt-J jobs")]
STEP_ORDER = ["auth", "select", "report", "plan", "prepare", "upgrade", "settings", "releases", "jobs"]       # Alt-1 .. Alt-9
PANE_FROM = 120                  # a window this wide gets the jobs pane on the right
FORM_PANE_FROM = 170             # the forms (sign-in table, settings) need their room, so they only share a very wide window
KEPT_JOBS = 30                   # finished jobs remembered for the Jobs screen
LOG_LIMIT = 5000                 # progress notes kept per job
RECENT = 5                       # lines of a job's log shown in the pane
PLAN_MAX_AGE = 30 * 60           # a prepare plan older than this (seconds) must be read again before it can be approved


def cut(line, width):
    out, used = [], 0
    for style, text in line:
        text = text[:max(width - used, 0)]
        used += len(text)
        if text:
            out.append((style, text))
    return out


def wrapped(style, text, width, indent=" "):
    import textwrap
    return [[(style, line)] for line in textwrap.wrap(text, max(width - 2, 20), initial_indent=indent, subsequent_indent=indent,
                                                       break_on_hyphens=False)] or [[]]


def width_of(line):
    return sum(len(text) for _style, text in line)


def ago(seconds):
    seconds = max(int(seconds), 0)
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


def clock(moment, seconds=True):
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(moment))


# ---- background work ---------------------------------------------------------------------------------------------------------

class _Router:
    """Stands in for sys.stdout / sys.stderr while jobs run: each job's thread writes into its own buffer, every other thread (the
    screen) to the real stream. Without it two jobs at once would pour into each other's capture."""

    def __init__(self, base):
        self.base, self.targets = base, {}

    def _target(self):
        return self.targets.get(threading.get_ident(), self.base)

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


_capture_lock = threading.Lock()
_capture = {"count": 0, "out": None, "err": None}


def _capture_start(out, err):
    with _capture_lock:
        if _capture["count"] == 0:
            _capture["out"], _capture["err"] = _Router(sys.stdout), _Router(sys.stderr)
            sys.stdout, sys.stderr = _capture["out"], _capture["err"]
        _capture["count"] += 1
        me = threading.get_ident()
        _capture["out"].targets[me], _capture["err"].targets[me] = out, err


def _capture_stop():
    with _capture_lock:
        me = threading.get_ident()
        _capture["out"].targets.pop(me, None)
        _capture["err"].targets.pop(me, None)
        _capture["count"] -= 1
        if _capture["count"] == 0:
            if sys.stdout is _capture["out"]:
                sys.stdout = _capture["out"].base
            if sys.stderr is _capture["err"]:
                sys.stderr = _capture["err"].base


class Job:
    """A function run in a thread with everything it prints captured. Screens and the jobs pane poll it; it never touches the terminal."""

    def __init__(self, fn, title="", done=None, changes=False):
        self.title, self.done, self.reported, self.changes = title or "work", done, False, changes      # changes: it alters real firewalls
        self.started, self.finished, self.text, self.code, self.value = time.monotonic(), None, "", None, None
        self.began = time.time()
        self.stage = ""                                   # the latest words from progress.say
        self.activity = []                                # every progress note, with the time: [(HH:MM:SS, words, is a stage)]
        self.phase_text = ""                              # the named stage it is in now ("Reading firewall 3 of 6: e2")
        self.findings = []                                # what it reported as a problem or a caution: [("fail" | "warn", words)]
        self.abort_requested = False
        self._out, self._err = io.StringIO(), io.StringIO()
        self._fn = fn
        threading.Thread(target=self._run, daemon=True).start()

    def abort(self):
        """Ask read-only work to stop before its next call to Panorama. Work that changes firewalls is never interrupted this way."""
        if self.running and not self.changes:
            self.abort_requested = True
            return True
        return False

    @property
    def aborted(self):
        return self.abort_requested and self.code == 130

    def _note(self, text, is_stage=False):
        if self.abort_requested:
            raise progress.Aborted()
        self.stage = text
        if not self.activity or self.activity[-1][1] != text:
            self.activity.append((time.strftime("%H:%M:%S"), text, is_stage))
            del self.activity[:-LOG_LIMIT]

    def _phase(self, text):
        self._note(text, True)
        self.phase_text = text

    def _finding(self, kind, text):
        self.findings.append((kind, text))

    def _run(self):
        out, err = self._out, self._err
        _capture_start(out, err)
        progress.listen(self._note, self._phase, self._finding)
        progress.set_output(out)
        try:
            self.value = self._fn()
            self.code = self.value if isinstance(self.value, int) else 0
        except progress.Aborted:
            self.code = 130
            err.write("aborted by you\n")
        except (SourceError, OSError, ValueError) as exc:    # the kind of trouble a person can act on
            self.code = 2
            err.write(f"error: {exc}\n")
        except BaseException as exc:                     # a bug must show up on the screen, not kill the program
            self.code = 2
            err.write(f"internal error: {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3))
        finally:
            progress.listen(None)
            progress.set_output(None)
            _capture_stop()
        self.text = out.getvalue() + (("\n" if out.getvalue() else "") + err.getvalue() if err.getvalue() else "")
        self.finished = time.monotonic()

    @property
    def running(self):
        return self.finished is None

    @property
    def failed(self):
        return not self.running and bool(self.code)

    def elapsed(self):
        return int((self.finished or time.monotonic()) - self.started)

    def live(self):
        """Everything printed so far, while it is still running; the whole text once it has finished."""
        if not self.running:
            return self.text
        return self._out.getvalue() + self._err.getvalue()

    def log(self, stamps=True):
        """The whole history: what it was doing (with the time, unless ``stamps`` is off), in order, then everything it printed."""
        lines = [f"{when}  {words}" if stamps else words for when, words, _stage in self.activity]
        text = self.live().rstrip("\n")
        if text:
            lines += ([""] if lines else []) + text.split("\n")
        return lines

    def recent(self, count=5):
        """The last few lines of the log that say anything. When the work names its stages those are what is worth seeing, not every call."""
        stages = [words for _when, words, is_stage in self.activity if is_stage]
        if stages and self.running:
            return stages[-count:]
        return [line for line in self.log(stamps=False) if line.strip()][-count:]

    def words(self):
        """What to show beside the spinner: the stage it is in, else the latest thing it did."""
        return (self.phase_text if self.running else "") or self.latest()

    def latest(self):
        """A few words on what it is doing now (running) or how it ended."""
        if self.running:
            if self.stage:
                return self.stage
            lines = [line.strip() for line in self.live().splitlines() if line.strip()]
            return lines[-1] if lines else "starting..."
        if self.failed:
            lines = [line.strip() for line in self.text.splitlines() if line.strip()]
            return lines[-1] if lines else f"ended with code {self.code}"
        return "finished"


# ---- session -----------------------------------------------------------------------------------------------------------------

class Session:
    """What the person has told the shell so far, shared by every screen: the Panorama profile in use, its address and target version."""

    def __init__(self, base, address="", target="", profile=None):
        self.base, self.address, self.target, self.profile = base, address, target, profile

    def args_for(self, command, *extra, address=True):
        argv = [command] + (["--profile", self.profile.name] if self.profile else [])
        argv += ["--panorama", self.address.strip()] if address and self.address.strip() else []
        argv += ["--color", "never", "--ascii", *extra]
        if getattr(self.base, "creds_file", None):
            argv += ["--creds-file", self.base.creds_file]
        args = cli.parse_args(argv)
        args._in_shell = True
        return args

    def base_args(self):
        """Arguments for questions that need no Panorama call (where the credentials are, which settings file)."""
        return self.args_for("auth")

    def creds_path(self):
        """The credentials file in use: the one named on the command line, else the environment's, else the profile's."""
        return getattr(self.base, "creds_file", None) or site.creds_path(self.profile)[0]

    def ws(self, name):
        """A file in the working place: the profile's workspace, or the current folder when there is no profile."""
        return self.profile.path(name) if self.profile else Path(name)

    def use(self, name):
        """Switch to another profile: its address and target replace the ones shown."""
        self.persist()
        self.profile = profiles.load(name)
        self.address, self.target = (self.profile.address, self.profile.target) if self.profile else ("", "")
        profiles.remember(name)

    def persist(self):
        """Keep the address and target typed here: in the profile when there is one, else as what was typed last."""
        if self.profile is not None:
            if (self.profile.address, self.profile.target) != (self.address.strip(), self.target.strip()):
                self.profile.address, self.profile.target = self.address, self.target
                self.profile.save()
        else:
            site.write_state(panorama=self.address.strip(), target=self.target.strip())


# ---- screens ---------------------------------------------------------------------------------------------------------------------

class Screen:
    name, title, tick_s = "", "", None
    pane_from = PANE_FROM                            # how wide the window must be for the jobs pane to fit beside this screen

    def __init__(self, shell):
        self.shell = shell
        self.pending = None                          # a sign-in or certificate problem a finished job left, to deal with on the next visit
        self.retry = False                           # set when the person fixed the sign-in the step asked for: read again on arrival, once

    def enter(self):
        pass

    def reset(self):
        """Forget what was read, so the next visit reads again."""

    def profile_changed(self):
        """Another Panorama profile was chosen."""
        self.reset()

    def tick(self):
        return False

    def dirty(self):
        return False

    def settle_problem(self, args):
        """After a job: a certificate or sign-in problem takes the person to the sign-in screen (now when this screen is in front,
        otherwise on their next visit). True when there was one."""
        problem = getattr(args, "_certificate_problem", None)
        signin = getattr(args, "_sign_in_problem", "")
        if not (problem or signin):
            return False
        self.pending = ("certificate", problem) if problem else ("sign-in", signin)
        if self.shell.screen is self:
            self.route_pending()
        else:
            self.shell.note = f"{self.title} needs you to sign in again (Alt-{self.shell.number(self.name)})."
        return True

    def route_pending(self):
        kind, what = self.pending
        self.pending = None
        (self.shell.need_certificate if kind == "certificate" else self.shell.need_sign_in)(self, what)


class FormScreen(Screen):
    """One of the existing forms (sign-in, settings) shown full size; it ends when the form does."""
    pane_from = FORM_PANE_FROM

    def __init__(self, shell, name, title, make):
        super().__init__(shell)
        self.name, self.title, self._make = name, title, make
        self.form, self.problem = None, ""

    def enter(self):
        self.form, self.problem = self._make()

    def dirty(self):
        return bool(self.form and self.form.dirty())

    def render(self, width, height):
        if self.form is None:
            return [[("error", " " + self.problem)]] if self.problem else [[]]
        return self.form.render(width, height, f"panupgrade - {self.title}")

    def press(self, key):
        if self.form is None:
            return "home"
        result = self.form.press(key)
        if result:
            return self.shell.form_finished(self, result)
        return None


class Record:
    """What one finished read said, kept so the screen can show it again."""

    def __init__(self, job):
        self.text, self.code, self.seconds, self.when = job.text, job.code, job.elapsed(), time.time()
        self.lines = job.text.rstrip("\n").split("\n")


class JobScreen(Screen):
    """A step that reads Panorama and shows what the matching command prints: the fleet report, the plan.

    The last answer for each (profile, address, target) is kept, so leaving the screen and coming back shows it again, marked with how old
    it is. Refresh reads again; while it does, the old answer stays on show."""

    def __init__(self, shell, name, title, command, needs_target=False, intro="", needs_address=True, auto=False):
        super().__init__(shell)
        self.name, self.title, self.command, self.needs_target, self.intro = name, title, command, needs_target, intro
        self.needs_address = needs_address
        self.auto = auto                                  # read as soon as the screen opens: only for things that touch no network
        self.records, self.running = {}, {}               # by key: the last answer, and the read in flight
        self.offset, self.bar, self.note = 0, 0, ""

    def key(self, session):
        return (session.profile.name if session.profile else "", session.address if self.needs_address else "",
                session.target if self.needs_target else "")

    def reset(self):
        self.records.clear()
        self.running.clear()                              # reads still in flight are left to finish; their answers are not wanted here
        self.pending, self.note = None, ""

    def profile_changed(self):
        self.pending, self.note, self.offset = None, "", 0   # each profile keeps its own answers

    def record(self, session=None):
        return self.records.get(self.key(session or self.shell.session))

    def reading(self, session=None):
        return self.running.get(self.key(session or self.shell.session))

    def enter(self):
        if self.pending:
            self.route_pending()
            return
        key = self.key(self.shell.session)
        self.offset = 0
        retry, self.retry = self.retry, False
        if (self.auto or retry) and key not in self.records and key not in self.running:
            self.start()

    def start(self):
        session = self.shell.session
        self.note = ""
        if self.needs_address and not session.address.strip():
            self.note = "Choose or make a Panorama profile, or type its address, on the home screen first (Alt-H)."
            return
        if self.needs_target and not session.target.strip():
            self.note = "Type the target PAN-OS version on the home screen first (Alt-H)."
            return
        key = self.key(session)
        if key in self.running:
            return
        args = session.args_for(self.command, *(["--target", session.target.strip()] if self.needs_target else []), address=self.needs_address)
        where = session.profile.name if session.profile else session.address.strip()
        title = f"{self.title.capitalize()}" + (f" ({where})" if self.needs_address and where else "")
        self.running[key] = self.shell.start_job(title, lambda: self.shell.run_command(args), lambda job: self._finished(job, args, key))

    def abort(self):
        job = self.reading()
        if job is not None and job.abort():
            self.note = "Aborting: it stops before its next call to Panorama (a call already in progress has to return first)."

    def buttons(self):
        """What the bar offers: Start before anything was read, Abort while it reads, Refresh once there is an answer."""
        if self.reading() is not None:
            first = ("Abort", self.abort)
        elif self.record() is not None:
            first = ("Refresh", self.start)
        else:
            first = ("Start", self.start)
        return [first, ("Change sign-in", "sign-in"), ("Home", "home")]

    def _finished(self, job, args, key):
        if self.running.get(key) is not job:              # it was reset while this ran
            return
        del self.running[key]
        if job.aborted:                                   # nothing new was learned, so what was shown before stays
            self.note = "Aborted. " + ("The earlier result is still shown." if key in self.records else "Nothing was read.")
            return
        self.records[key] = Record(job)
        if self.shell.screen is self and key == self.key(self.shell.session):
            self.offset = 0
        self.settle_problem(args)

    def render(self, width, height):
        job, record = self.reading(), self.record()
        lines = [[("bold", f" panupgrade - {self.title}")]]
        if self.intro:
            lines += wrapped("dim", self.intro, width)
        if self.note:
            lines += wrapped("error", self.note, width)
        if job is None and record is None and not self.note:
            lines += wrapped("dim", "Nothing has been read yet. Press Start to read it; this only reads, it changes nothing.", width)
        if job is not None:
            seen = f"   (the result from {clock(record.when)} stays on show until this finishes)" if record else ""
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} working... {job.elapsed()}s {job.words()}"), ("dim", seen)])
        elif record is not None:
            ok = not record.code
            lines.append([("" if ok else "error", f" result from {clock(record.when)} ({ago(time.time() - record.when)}), finished in {record.seconds}s"
                           + ("" if ok else f" with an error (code {record.code})")), ("dim", "   R refreshes")])
        lines.append([])
        bar = [("", " ")]
        for n, (label, _action) in enumerate(self.buttons()):
            bar += [("focus" if self.bar == n else "bold", f"[ {label} ]"), ("", "  ")]
        body_lines = record.lines if record is not None else []
        room = max(height - len(lines) - 2, 3)
        self.offset = max(0, min(self.offset, max(len(body_lines) - room, 0)))
        body = [[("", " " + line)] for line in body_lines[self.offset:self.offset + room]]
        if len(body_lines) > room:
            lines += body + [[("dim", f" lines {self.offset + 1}-{min(self.offset + room, len(body_lines))} of {len(body_lines)}  (Up / Down / PgUp / PgDn scroll)")]]
        else:
            lines += body
        lines += [[]] * max(height - len(lines) - 1, 0)
        lines.append(bar)
        return [cut(line, width - 1) for line in lines[:height]]

    def press(self, key):
        page = 10
        count = len(self.record().lines) if self.record() else 0
        if key == "up":
            self.offset = max(self.offset - 1, 0)
        elif key == "down":
            self.offset += 1
        elif key == "pgup":
            self.offset = max(self.offset - page, 0)
        elif key == "pgdn":
            self.offset += page
        elif key == "home":
            self.offset = 0
        elif key == "end":
            self.offset = count
        elif key in ("r", "R", "f5"):
            if self.reading() is None:                            # while it reads, the first button is Abort and R does nothing
                self.start()
        elif key in ("left", "shift-tab"):
            self.bar = max(self.bar - 1, 0)
        elif key in ("right", "tab"):
            self.bar = min(self.bar + 1, 2)
        elif key == "enter":
            action = self.buttons()[min(self.bar, 2)][1]
            if action == "sign-in":
                self.shell.after_sign_in = self.name              # signing in again, then straight back here to read again
                return ("goto", "auth")
            if action == "home":
                return "home"
            action()
        elif key == "esc":
            return "home"
        return None


class SelectScreen(Screen):
    """Choose firewalls: read the list from Panorama in the background, then the select form."""
    name, title = "select", "choose the firewalls"

    def __init__(self, shell):
        super().__init__(shell)
        self.job, self.form, self.note, self.args = None, None, "", None

    @property
    def pane_from(self):
        return FORM_PANE_FROM if self.form is not None else PANE_FROM

    def dirty(self):
        return bool(self.form and self.form.dirty())

    def reset(self):
        self.job, self.form = None, None

    def enter(self):
        session = self.shell.session
        if self.pending:
            self.route_pending()
            return
        if self.form is not None and not self.form.dirty():
            self.form = None
        if self.form is not None or (self.job is not None and self.job.running):
            return
        if not session.address.strip():
            self.note = "Choose or make a Panorama profile, or type its address, on the home screen first (Alt-H)."
            return
        self.note = ""
        self.args = args = session.args_for("select")
        where = session.profile.name if session.profile else session.address.strip()
        self.job = self.shell.start_job(f"Firewall list ({where})", lambda: self.shell.read_devices(args), lambda job: self._finished(job, args))

    def _finished(self, job, args):
        if self.job is not job:                           # reset while it ran
            return
        if self.settle_problem(args):
            return
        if job.code or not isinstance(job.value, list):
            self.note = (job.text or "could not read the firewall list").strip()
        else:
            self.form = cli._select_form(args, job.value)

    def render(self, width, height):
        if self.form is not None:
            return self.form.render(width, height, "panupgrade - choose the firewalls")
        lines = [[("bold", " panupgrade - choose the firewalls")], []]
        if self.job is not None and self.job.running:
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} reading the firewall list from Panorama... {self.job.elapsed()}s {self.job.words()}")])
        elif self.note:
            lines += wrapped("error", self.note, width)
            lines += wrapped("dim", "Alt-H goes home; Alt-A changes how this signs in; Enter tries again.", width)
        return [cut(line, width - 1) for line in lines[:height]]

    def press(self, key):
        if self.form is None:
            if key == "enter":
                self.enter()
            elif key == "esc":
                return "home"
            return None
        result = self.form.press(key)
        if result:
            return self.shell.form_finished(self, result)
        return None


class TargetScreen(Screen):
    """Choose the target from what a firewall itself lists as available: pick a firewall of the fleet, then one of its versions. The home
    screen's text field still takes any version typed by hand; this is for when the exact spelling (hotfix suffixes, dots, dashes) matters."""
    name, title = "targets", "choose the target from a firewall's list"

    def __init__(self, shell):
        super().__init__(shell)
        self.reset()

    def reset(self):
        self.job, self.args, self.refs, self.chosen, self.versions, self.at, self.note = None, None, None, None, None, 0, ""

    def enter(self):
        if self.pending:
            self.route_pending()
            return
        self.at, self.note = 0, ""
        self.chosen, self.versions = None, None
        session = self.shell.session
        if self.refs is not None or (self.job is not None and self.job.running):
            return
        if not session.address.strip():
            self.note = "Choose or make a Panorama profile, or type its address, on the home screen first (Alt-H)."
            return
        self.args = args = session.args_for("select")
        where = session.profile.name if session.profile else session.address.strip()
        self.job = self.shell.start_job(f"Firewall list ({where})", lambda: self.shell.read_devices(args), lambda job: self._fleet_read(job, args))

    def _fleet_read(self, job, args):
        if self.job is not job:
            return
        self.job = None
        if self.settle_problem(args):
            return
        if job.code or not isinstance(job.value, list):
            self.note = (job.text or "could not read the firewall list").strip()
            return
        self.refs = sorted((r for r in job.value if r.connected), key=lambda r: r.hostname.lower())
        if not self.refs:
            self.note = "No firewall is connected to Panorama, so there is none to ask."

    def _ask(self, ref):
        self.chosen, self.versions, self.at, self.note = ref, None, 0, ""
        args = self.shell.session.args_for("select")
        self.job = self.shell.start_job(f"Versions offered by {ref.hostname}", lambda: self.shell.read_versions(args, ref.serial),
                                        lambda job: self._versions_read(job, args))

    def _versions_read(self, job, args):
        if self.job is not job:
            return
        self.job = None
        if self.settle_problem(args):
            return
        if job.code or not isinstance(job.value, list):
            self.note = (job.text or "could not read the firewall's software list").strip()
            self.chosen = None
        elif not job.value:
            self.note = f"{self.chosen.hostname} lists no versions. Pick another firewall, or type the version on the home screen."
            self.chosen = None
        else:
            self.versions = job.value

    def items(self):
        return self.versions if self.versions is not None else (self.refs or [])

    def render(self, width, height):
        lines = [[("bold", " panupgrade - choose the target from a firewall's list")], []]
        busy = self.job is not None and self.job.running
        if busy:
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} {self.job.elapsed()}s {self.job.title}: {self.job.words()}")])
        if self.note:
            lines += wrapped("error", self.note, width)
        if self.versions is None and self.refs is None:
            if not busy:
                lines += wrapped("dim", "Enter tries again; Esc goes home.", width)
            return [cut(line, width - 1) for line in lines[:height]]
        if self.versions is None:
            lines += wrapped("dim", "Pick a firewall; it will be asked which PAN-OS versions it offers. Up / Down choose, Enter asks, Esc goes home.", width)
        else:
            lines += wrapped("dim", f"Versions {self.chosen.hostname} ({self.chosen.sw_version or 'version unknown'}) lists, newest first. Enter makes one the target; "
                                    f"Esc goes back to the firewalls.", width)
        lines.append([])
        room = max(height - len(lines) - 1, 3)
        items = self.items()
        top = max(0, min(self.at - room // 2, len(items) - room))
        for n in range(top, min(top + room, len(items))):
            on = n == self.at
            item = items[n]
            if self.versions is None:
                text = f"{item.hostname.ljust(24)} {item.serial.ljust(16)} {item.sw_version or '?'}" + (f"  {item.ha_state}" if item.ha_state else "")
            else:
                tags = [t for t, flag in (("current", item["current"]), ("downloaded", item["downloaded"]), ("latest", item["latest"])) if flag]
                text = f"{item['version'].ljust(16)} {', '.join(tags).ljust(26)} {item['released']}"
            lines.append([("focus" if on else "", f" {'>' if on else ' '} {text}")])
        if len(items) > room:
            lines.append([("dim", f" {self.at + 1} of {len(items)}  (PgUp / PgDn scroll)")])
        return [cut(line, width - 1) for line in lines[:height]]

    def press(self, key):
        items = self.items()
        if key == "esc":
            if self.versions is not None and not (self.job is not None and self.job.running):
                self.versions, self.chosen, self.at = None, None, 0
                return None
            return "home"
        if self.job is not None and self.job.running:
            return None
        if self.refs is None and self.versions is None:
            if key == "enter":
                self.enter()
            return None
        if key in ("up", "down", "pgup", "pgdn", "home", "end") and items:
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10}.get(key)
            self.at = 0 if key == "home" else len(items) - 1 if key == "end" else max(0, min(self.at + step, len(items) - 1))
        elif key == "enter" and items:
            if self.versions is None:
                self._ask(items[self.at])
            else:
                chosen = items[self.at]["version"]
                self.shell.session.target = chosen
                self.shell.message = f"Target set to {chosen}, as {self.chosen.hostname} lists it."
                return "home"
        return None


class ChangeScreen(Screen):
    """A step that changes firewalls, in the same three steps the command takes: read the plan (changes nothing), approve it by typing
    APPROVE, then run it. Leaving the screen does not stop a run; the Jobs screen shows it and quitting asks first.

    Prepare and Upgrade differ only in what they read, what they list for approval and a few extra buttons; everything that keeps them
    safe (the typed word, the approval bound to the plan that was read, the plan's age, the quit question) is here once."""
    verb = ""
    INTRO = ""

    def __init__(self, shell):
        super().__init__(shell)
        self.stage, self.job, self.bundle, self.args, self.key_ = "idle", None, None, None, None
        self.lines, self.offset, self.bar, self.note, self.typed = [], 0, 0, "", ""
        self.finished, self.result, self.mode = None, None, None            # mode: how an unfinished earlier run is dealt with

    # -- what differs
    def extra_args(self):
        return []

    def tasks(self):
        raise NotImplementedError

    def approval_body(self):
        raise NotImplementedError

    def approver(self):
        raise NotImplementedError

    def summary(self):
        raise NotImplementedError

    def review_buttons(self):
        raise NotImplementedError

    def running_buttons(self):
        """A real run is stopped at a step boundary, never torn down in the middle of a step; a rehearsal can simply be aborted."""
        if self.job is not None and self.job.changes and self.bundle is not None:
            return [("Stop after the steps in progress", self.stop), ("Home (it keeps running)", None)]
        return [("Abort", self.abort), ("Home (it keeps running)", None)]

    def stop(self):
        if self.bundle is not None and self.stage == "running":
            self.bundle.stop.set()
            self.note = ("Stopping: steps already going will finish and nothing new starts. The journal records where it got to; "
                         "read the plan again later to continue it.")

    def custom_body(self, width, room):
        """Styled lines to show instead of the plain text body, or None."""
        return None

    def prepare_run_args(self, args):
        """Last changes to the arguments of the real run (which earlier run to carry on, for instance)."""

    # -- where it stands
    def key(self, session):
        return (session.profile.name if session.profile else "", session.address.strip(), session.target.strip(), tuple(self.extra_args()))

    def busy(self):
        return self.stage in ("reading", "running")

    def reset(self):
        if self.busy():
            return                                       # never lose track of work that is going on
        self.stage, self.job, self.bundle, self.lines, self.note, self.typed, self.mode = "idle", None, None, [], "", "", None

    def profile_changed(self):
        self.reset()

    def enter(self):
        if self.pending:
            self.route_pending()
            return
        if self.stage in ("review", "approving") and self.key_ != self.key(self.shell.session):
            self.reset()                                 # the plan was for another address or target
        retry, self.retry = self.retry, False
        if retry and self.stage == "idle":
            self.read_plan()                             # the person had started this and was sent to fix the sign-in: carry on, once

    def tick(self):
        return self.busy()

    def abort(self):
        if self.stage == "reading" and self.job is not None and self.job.abort():
            self.note = "Aborting: it stops before its next call to Panorama (a call already in progress has to return first)."
        elif self.stage == "running" and self.job is not None and self.job.abort():           # a rehearsal; real runs are stopped, not aborted
            self.note = "Aborting the rehearsal."

    def where(self):
        session = self.shell.session
        return session.profile.name if session.profile else session.address.strip()

    # -- the three steps
    def _check(self):
        session = self.shell.session
        if not session.address.strip():
            return "Choose or make a Panorama profile, or type its address, on the home screen first (Alt-H)."
        if not session.target.strip():
            return "Type the target PAN-OS version on the home screen first (Alt-H)."
        return ""

    def read_plan(self):
        if self.busy():
            return
        self.note = self._check()
        if self.note:
            return
        session = self.shell.session
        self.args = args = session.args_for(self.verb, "--target", session.target.strip(), *self.extra_args())
        self.key_ = self.key(session)
        self.lines, self.offset, self.bundle, self.typed, self.mode = [], 0, None, "", None
        self.stage = "reading"
        review = getattr(self.shell, f"{self.verb}_review")
        self.job = self.shell.start_job(f"{self.verb.capitalize()} plan ({self.where()})", lambda: review(args), self._planned)

    def _planned(self, job):
        if self.job is not job or self.stage != "reading":
            return
        self.lines = job.text.rstrip("\n").split("\n") if job.text else []
        self.offset = 0
        if job.aborted:
            self.stage, self.bundle, self.lines, self.note = "idle", None, [], "Aborted. Nothing was changed."
        elif self.settle_problem(self.args):
            self.stage = "idle"
        elif job.code or not hasattr(job.value, "plan"):
            self.stage, self.bundle = "failed", None
        else:
            self.stage, self.bundle = "review", job.value
        self.bar = 0

    def begin_approval(self, mode=None):
        age = time.time() - self.bundle.created
        if age > PLAN_MAX_AGE:
            self.note = f"The plan was read {ago(age)[:-4]} ago and the fleet may have changed. Read it again before approving."
            return
        self.note, self.typed, self.stage, self.offset, self.mode = "", "", "approving", 0, mode

    def run(self):
        """Typed APPROVE: carry out exactly the plan that was read."""
        bundle = self.bundle
        if time.time() - bundle.created > PLAN_MAX_AGE:
            self.stage, self.note = "review", "That plan is now too old to approve. Read it again."
            return
        args, approve = self.args, self.approver()
        self.prepare_run_args(args)
        self.stage, self.note, self.offset, self.lines = "running", "", 0, []
        execute = getattr(self.shell, f"{self.verb}_execute")
        self.job = self.shell.start_job(f"{self.verb.capitalize()} ({self.where()})", lambda: execute(args, bundle, approve), self._ran, changes=True)

    def rehearse(self):
        """A dry run: the same steps against a model of the fleet. Nothing is sent to any firewall and nothing is asked."""
        session = self.shell.session
        args = session.args_for(self.verb, "--target", session.target.strip(), "--dry-run", *self.extra_args())
        self.args, self.stage, self.note, self.offset, self.lines = args, "running", "", 0, []
        self.job = self.shell.start_job(f"{self.verb.capitalize()} rehearsal ({self.where()})", lambda: self.shell.run_command(args), self._ran)

    def _ran(self, job):
        if self.job is not job or self.stage != "running":
            return
        if job.aborted:
            self.stage, self.lines, self.note = "idle", [], "Aborted. Nothing was changed."
            return
        self.stage, self.lines, self.offset, self.result, self.finished = "done", job.text.rstrip("\n").split("\n"), 0, job.code, time.time()
        self.bundle, self.bar, self.mode = None, 0, None
        for name in ("report", "plan", "prepare", "upgrade"):
            if self.shell.screens[name] is not self:
                self.shell.screens[name].reset()           # the fleet has changed, so what was read before is out of date

    # -- drawing
    def buttons(self):
        if self.stage == "review":
            return self.review_buttons()
        if self.stage == "running":
            return self.running_buttons()
        if self.stage == "reading":
            return [("Abort", self.abort), ("Home (it keeps reading)", None)]
        if self.stage in ("done", "failed", "idle"):
            return [("Read the plan again" if self.stage != "idle" else "Start: read the plan", self.read_plan), ("Home", None)]
        return [("Home", None)]

    def body(self):
        if self.stage == "approving":
            return self.approval_body()
        if self.stage == "running" and self.job is not None:
            text = self.job.live().rstrip("\n")
            return text.split("\n") if text else []
        return self.lines

    def render(self, width, height):
        lines = [[("bold", f" panupgrade - {self.verb}")]] + (wrapped("dim", self.INTRO, width) if height >= 24 else [])
        if self.note:
            lines += wrapped("error", self.note, width)
        spin = SPIN[int(time.monotonic() * 4) % 4]
        job = self.job
        if self.stage == "reading":
            lines.append([("warn", f" {spin} {job.elapsed()}s  {job.words() if job.phase_text else 'starting: reading the fleet and building the plan...'}")])
        elif self.stage == "review":
            lines += wrapped(*self.summary(), width)
        elif self.stage == "approving":
            lines.append([("warn", " Nothing starts until you type APPROVE below. Esc goes back without doing anything.")])
        elif self.stage == "running":
            lines.append([("warn", f" {spin} {'running' if job.changes else 'rehearsing'}... {job.elapsed()}s {job.words()}"),
                          ("dim", "   leaving this screen does not stop it; Alt-J shows it")])
        elif self.stage == "done":
            ok = not self.result
            lines.append([("ok" if ok else "error", f" finished at {clock(self.finished)} ({ago(time.time() - self.finished)})"
                           + ("" if ok else f" with an error (code {self.result}); read the output below"))])
        elif self.stage == "failed":
            lines.append([("error", " The plan could not be built; the reason is below.")])
        lines.append([])
        if self.stage == "approving":
            footer = [[("bold", " Type APPROVE to go ahead: "), ("focus", f"[{self.typed}_]".ljust(14)), ("dim", "   Enter confirms, Esc goes back")]]
        else:
            footer = self._bar_lines(width)
        lines = lines[:max(height - len(footer) - 4, 2)]
        room = max(height - len(lines) - len(footer) - 1, 3)
        shown = self.custom_body(width, room)
        if shown is None:
            body_lines = self._wrap_all(self.body(), width - 3)
            self.offset = max(0, min(self.offset, max(len(body_lines) - room, 0)))
            shown = [[("", " " + line)] for line in body_lines[self.offset:self.offset + room]]
            if len(body_lines) > room:
                shown.append([("dim", f" lines {self.offset + 1}-{min(self.offset + room, len(body_lines))} of {len(body_lines)}  (Up / Down / PgUp / PgDn scroll)")])
        lines += shown
        lines = lines[:max(height - len(footer), 0)]
        lines += [[]] * max(height - len(lines) - len(footer), 0)
        lines += footer
        return [cut(line, width - 1) for line in lines[:height]]

    def _bar_lines(self, width):
        """The buttons, wrapped onto as many lines as the window needs."""
        rows, row, used = [], [("", " ")], 1
        for n, (label, _action) in enumerate(self.buttons()):
            piece = f"[ {label} ]"
            if used + len(piece) + 2 > width - 1 and used > 1:
                rows.append(row)
                row, used = [("", " ")], 1
            row += [("focus" if self.bar == n else "bold", piece), ("", "  ")]
            used += len(piece) + 2
        return rows + [row]

    @staticmethod
    def _wrap_all(lines, width):
        """Long lines wrap (under their own indent) instead of being cut off: what is being approved must be readable in full."""
        import textwrap
        out = []
        for line in lines:
            indent = " " * (len(line) - len(line.lstrip()))
            out += textwrap.wrap(line, max(width, 20), subsequent_indent=indent + "  ", break_on_hyphens=False, drop_whitespace=False) or [""]
        return [line.rstrip() for line in out]

    # -- keys
    def press(self, key):
        if self.stage == "approving":
            return self._approving(key)
        count = len(self.body()) * 3
        if key == "up":
            self.offset = max(self.offset - 1, 0)
        elif key == "down":
            self.offset += 1
        elif key == "pgup":
            self.offset = max(self.offset - 10, 0)
        elif key == "pgdn":
            self.offset += 10
        elif key == "home":
            self.offset = 0
        elif key == "end":
            self.offset = count
        elif key == "esc":
            return "home"
        elif key in ("left", "shift-tab"):
            self.bar = max(self.bar - 1, 0)
        elif key in ("right", "tab"):
            self.bar = min(self.bar + 1, len(self.buttons()) - 1)
        elif key in ("enter", " "):
            buttons = self.buttons()
            action = buttons[min(self.bar, len(buttons) - 1)][1]
            if action is None:
                return "home"
            self.bar = 0
            action()
        return None

    def _approving(self, key):
        if key == "esc":
            self.stage, self.typed, self.note, self.offset = "review", "", "", 0
        elif key == "enter":
            if self.typed.strip().upper() == "APPROVE":
                self.run()
            else:
                self.note = "Nothing was started. Type the word APPROVE exactly to go ahead, or press Esc to go back."
        elif key == "backspace":
            self.typed = self.typed[:-1]
        elif key == "ctrl-u":
            self.typed = ""
        elif key == "pgup":
            self.offset = max(self.offset - 10, 0)
        elif key == "pgdn":
            self.offset += 10
        elif len(key) == 1 and key.isprintable():
            self.typed += key
        return None


FLEET = [{"serial": "", "hostname": "Whole fleet", "fleet": True}]       # the list's first entry: everything that is not one firewall's own steps


class UnitView:
    """The run, firewall by firewall, for a screen that changes firewalls: a list of units (an HA pair is one unit) with each one's own
    log beside it, or the combined output (V). The screen supplies ``units()`` (lists of dicts with a serial and a hostname) and
    ``unit_finished(unit, events)``; the bundle of a real run supplies ``events_snapshot()``."""

    def __init__(self, shell):
        super().__init__(shell)
        self.kept = None                                  # the bundle of the last real run, so its log can still be read after it ends
        self.view, self.unit_at, self.unit_pinned, self.log_offset = "units", 0, False, None

    def reset(self):
        if not self.busy():
            self.kept = None
        super().reset()

    def _ran(self, job):
        if self.job is job and self.stage == "running" and job.changes:
            self.kept = self.bundle
        super()._ran(job)

    def source(self):
        """The bundle whose events are shown: the one running now, or the last one that ran."""
        return self.bundle if self.stage == "running" and self.job is not None and self.job.changes else self.kept

    @staticmethod
    def step_name(record):
        return re.sub(r"^\d+:", "", record.get("step", ""))

    # -- the whole fleet: the stages around the firewalls' own steps, and every problem the run reported that belongs to no one firewall
    def all_units(self):
        units = self.units()
        return [FLEET] + units if units else []

    def fleet_output(self):
        job = self.job
        return [] if job is None else job.log()

    def fleet_problems(self):
        """(failures, warnings): what the run itself reported, as data, so a blocked plan or a failed verification is never only in the text."""
        if self.job is None:
            return [], []
        found = list(self.job.findings)
        failures = [f"[FAIL] {text}" for kind, text in found if kind == "fail"]
        if self.job.failed:
            failures.append(f"[FAIL] the run ended with code {self.job.code}")
        return failures, [f"[WARN] {text}" for kind, text in found if kind == "warn"]

    def fleet_state(self):
        failures, warnings = self.fleet_problems()
        if self.stage == "running":
            return "running", (self.job.words() if self.job is not None else "") or "working"
        if failures or any(self.unit_state(unit)[0] == "failed" for unit in self.units()):
            return "failed", f"{len(failures)} problem{'s' if len(failures) != 1 else ''} reported" + (f", {len(warnings)} warning(s)" if warnings else "") + ": read the log"
        if warnings:
            return "warn", f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}: read the log"
        return "done", "nothing else to report"

    def unit_log(self, unit):
        """The styled lines shown for one entry of the list."""
        if unit is FLEET:
            failures, warnings = self.fleet_problems()
            lines = [("error", line) for line in failures] + [("warn", line) for line in warnings]
            if lines:
                lines += [("", ""), ("dim", "Everything it printed, in order:")]
            return lines + [("", line) for line in self.fleet_output()] or [("dim", "(nothing has happened here yet)")]
        return [self.event_line(unit, when, rec) for when, rec in self.unit_events(unit)] or [("dim", "(nothing has happened here yet)")]

    def unit_events(self, unit):
        if unit is FLEET:
            return []
        serials = {d["serial"] for d in unit}
        return [(when, rec) for when, rec in self.source().events_snapshot() if rec.get("serial") in serials and rec.get("type") == "step"]

    def unit_state(self, unit):
        """(state, words): waiting, running, done or failed, and what it is on or where it went wrong."""
        if unit is FLEET:
            return self.fleet_state()
        events = self.unit_events(unit)
        final = self.stage == "done"
        if not events:
            return ("idle", "not started") if final else ("waiting", "waiting its turn")
        for _when, rec in events:
            if rec["state"] == "failed":
                return "failed", f"{self._name(unit, rec)}: {self.step_name(rec)}"
        if self.unit_finished(unit, events):
            return "done", "all steps done"
        if final:
            return "stopped", "stopped before it finished"
        _when, rec = events[-1]
        return "running", f"{self._name(unit, rec)}: {self.step_name(rec)}"

    @staticmethod
    def _name(unit, rec):
        return next((d["hostname"] for d in unit if d["serial"] == rec.get("serial")), rec.get("serial", ""))

    def event_line(self, unit, when, rec):
        detail = rec.get("detail") or {}
        extra = ", ".join(f"{k}={v}" for k, v in detail.items() if k in ("version", "sessions_before", "sessions_after", "ha_state", "synced_from",
                                                                         "skipped", "peer_version", "config_sync"))
        words = {"started": "started", "ok": "ok", "failed": "FAILED", "note": "note"}.get(rec["state"], rec["state"])
        text = f"{clock(when)}  {self._name(unit, rec).ljust(14)} {self.step_name(rec)}  {words}"
        if rec["state"] == "failed":
            text += f": {detail.get('error', '')}"
        elif extra:
            text += f"  {extra}"
        return ("error" if rec["state"] == "failed" else "dim" if rec["state"] == "started" else ""), text

    def shown_unit(self, units):
        if self.unit_pinned and self.unit_at < len(units):
            return self.unit_at
        states = [self.unit_state(unit)[0] for unit in units]
        if "running" in states[1:]:
            return states.index("running", 1)                 # follow the firewall that is being worked on
        for wanted in ("failed", "running", "warn"):          # a failure is what to look at; then the stage the whole run is in
            if wanted in states:
                return states.index(wanted)
        return len(units) - 1 if states and all(state == "done" for state in states) else 0

    def showing_units(self):
        """The by-firewall view is on: asked for (the default), there is a real run to show, and either it is still going or it left events.
        A run that ended without doing anything (refused, failed to start) shows its plain output, where the reason is."""
        source = self.source()
        if self.view != "units" or source is None or self.stage not in ("running", "done"):
            return False
        return self.stage == "running" or bool(source.events_snapshot())

    @property
    def pane_from(self):
        return FORM_PANE_FROM if self.showing_units() else PANE_FROM            # this view has its own list of units and wants the room

    def custom_body(self, width, room):
        units = self.all_units() if self.showing_units() else []
        if not units:
            return None
        at = self.shown_unit(units)
        listing = []
        for n, unit in enumerate(units):
            state, words = self.unit_state(unit)
            mark = {"waiting": ("dim", ".. "), "idle": ("dim", ".. "), "running": ("warn", ">> "), "done": ("ok", "ok "), "failed": ("error", "!! "),
                    "stopped": ("error", "-- "), "warn": ("warn", "?? ")}[state]
            label = " + ".join(d["hostname"] for d in unit)
            listing += [[("focus" if n == at else "bold", f" {'>' if n == at else ' '}"), mark, ("bold", label)],
                        [("", "     "), (mark[0] if state != "waiting" else "dim", words)]]
        unit = units[at]
        state, words = self.unit_state(unit)
        log = self.unit_log(unit)
        left = 40
        space = max(room - 3, 2)
        end = max(len(log) - space, 0)
        start = end if self.log_offset is None else max(0, min(self.log_offset, end))
        detail = [[("bold", f" {' + '.join(d['hostname'] for d in unit)}"), ("dim", f"   {state}: {words}")], []]
        detail += [[(style, " " + text)] for style, text in log[start:start + space]]
        detail.append([("dim", f" lines {start + 1}-{min(start + space, len(log))} of {len(log)}  (PgUp / PgDn scroll)")] if len(log) > space else [])
        if width < 100:                                   # two columns do not fit: the list, then the log
            listing = [row for row in listing][:max(room // 2, 2)]
            return (listing + [[]] + detail)[:room]
        rows = []
        for n in range(room):
            a = cut(listing[n], left - 1) if n < len(listing) else []
            b = detail[n] if n < len(detail) else []
            rows.append(a + [("", " " * max(left - width_of(a), 0)), ("dim", "| ")] + b)
        return rows

    def toggle_view(self):
        self.view = "text" if self.view == "units" else "units"

    def buttons(self):
        out = super().buttons()
        source = self.source()
        if self.stage in ("running", "done") and source is not None and (self.stage == "running" or source.events_snapshot()):
            out.insert(len(out) - 1, ("View: combined output" if self.view == "units" else "View: by firewall", self.toggle_view))
        return out

    def press(self, key):
        if self.stage in ("running", "done") and self.source() is not None:
            if key in ("v", "V"):
                self.toggle_view()
                return None
            if self.showing_units():
                units = self.all_units()
                at = self.shown_unit(units) if units else 0
                if key in ("up", "down") and units:
                    self.unit_at, self.unit_pinned, self.log_offset = max(0, min(at + (1 if key == "down" else -1), len(units) - 1)), True, None
                    return None
                if key in ("pgup", "pgdn", "home", "end"):
                    self._scroll_log(key, units[at] if units else None)
                    return None
        return super().press(key)

    visible = 12

    def _scroll_log(self, key, unit):
        total = len(self.unit_log(unit)) if unit else 0
        end = max(total - self.visible, 0)
        at = end if self.log_offset is None else min(self.log_offset, end)
        at = max(0, {"pgup": at - self.visible, "pgdn": at + self.visible, "home": 0, "end": end}[key])
        self.log_offset = None if at >= end else at



class PrepareScreen(UnitView, ChangeScreen):
    name, title, verb = "prepare", "prepare", "prepare"
    INTRO = ("Gets the firewalls ready ahead of the window: brings HA members' content level, forces a passive member's configuration to "
             "match its active, and downloads the images for the target. It changes the firewalls, so nothing starts until you have read the "
             "plan and typed APPROVE. It never installs PAN-OS, reboots or fails anything over.")

    def tasks(self):
        return bool(self.bundle and self.bundle.plan["serials"])

    def units(self):
        source = self.source()
        return [] if source is None else [list(group) for group in source.groups]

    @staticmethod
    def unit_finished(unit, events):
        """Every task planned for these firewalls has finished."""
        wanted = [(dev["serial"], step) for dev in unit for step in dev["expected"]]
        return bool(wanted) and all(any(rec["serial"] == serial and rec["step"] == step and rec["state"] == "ok" for _w, rec in events) for serial, step in wanted)

    def approval_body(self):
        from . import prepare as prepare_mod
        plan = self.bundle.plan
        return [cli._prepare_heading(plan), ""] + [f"  {line}" for line in prepare_mod.describe(plan)]

    def approver(self):
        plan = self.bundle.plan
        return lambda journal_path: cli._new_approval(plan["hash"], plan["serials"], [], journal_path, "typed APPROVE on the prepare screen")

    def summary(self):
        count = len(self.bundle.plan["serials"])
        return "ok", (f"Plan read at {clock(self.bundle.created)} ({ago(time.time() - self.bundle.created)}), {self.bundle.label}: "
                      + (f"{count} firewall{'s' if count != 1 else ''} to prepare. Nothing has been changed yet." if count
                         else "nothing to prepare. Nothing has been changed."))

    def review_buttons(self):
        tasks = self.tasks()
        return [("Approve and run..." if tasks else "Continue to the upgrade kit", self.begin_approval if tasks else self.run),
                ("Rehearse (dry run)", self.rehearse), ("Read the plan again", self.read_plan), ("Home", None)]

    def status(self):
        if self.stage == "reading":
            return "warn", "reading the plan..."
        if self.stage == "review":
            count = len(self.bundle.plan["serials"])
            return "warn", (f"plan ready: {count} firewall{'s' if count != 1 else ''} to prepare - waiting for your approval" if count
                            else "plan ready: nothing to prepare")
        if self.stage == "approving":
            return "warn", "waiting for you to type APPROVE"
        if self.stage == "running":
            return "warn", "RUNNING - changing firewalls now"
        if self.stage == "done":
            return ("ok" if not self.result else "error"), f"finished at {clock(self.finished, False)}" + ("" if not self.result else f" with an error (code {self.result})")
        return "dim", "not started - open it, then press Start"


class UpgradeScreen(UnitView, ChangeScreen):
    name, title, verb = "upgrade", "upgrade", "upgrade"
    INTRO = ("Upgrades the chosen firewalls. An HA pair is one unit and the passive member goes first; each member is backed up, installed, "
             "rebooted and checked, and the run stops by itself after too many failures. It reboots real firewalls, so nothing starts until you "
             "have read the plan and typed APPROVE. Stop lets the steps in progress finish and starts nothing new; a stopped run can be continued.")

    def __init__(self, shell):
        super().__init__(shell)
        self.standalone = False

    def extra_args(self):
        return ["--accept-standalone-outage"] if self.standalone else []

    def units(self):
        source = self.source()
        if source is None:
            return []
        devices = {d["serial"]: d for d in source.plan["devices"]}
        return [[devices[x] for x in unit if x in devices] for unit in source.plan["order"]]

    @staticmethod
    def unit_finished(unit, events):
        """Every member has done the last step of its plan."""
        done = []
        for dev in unit:
            steps = dev["steps"]
            last = f"{len(steps) - 1}:{steps[-1]['action']}" if steps else ""
            done.append(bool(steps) and any(rec["serial"] == dev["serial"] and rec["step"] == last and rec["state"] == "ok" for _w, rec in events))
        return all(done)

    def tasks(self):
        return bool(self.bundle and self.bundle.ready())

    def toggle_standalone(self):
        self.standalone = not self.standalone
        self.reset()
        self.read_plan()

    def prepare_run_args(self, args):
        if self.bundle.previous:
            if self.mode == "resume":
                args.resume = True
            else:
                args.fresh = True

    def approval_body(self):
        bundle, plan = self.bundle, self.bundle.plan
        names = {d["serial"]: d["hostname"] for d in plan["devices"]}
        ready = bundle.ready()
        out = ["Approval needed. You are being asked to approve exactly this:"]
        if bundle.label == "LIVE":
            out.append("LIVE: this changes real firewalls. Each member is rebooted; an HA pair fails over, passive member first.")
        out += [""] + [f"  {d['hostname']} ({d['serial']}) {d['ha_state'] or 'standalone'}  {d['current_version']} -> {plan['target']}" for d in ready]
        s = plan["summary"]
        out += ["", f"  {s['outage_steps']} outage step(s) and {s['failover_steps']} HA failover(s)."]
        out.append(f"  Each unit is worked in series; up to {plan['settings']['max_parallel']} units run at the same time.")
        for n, unit in enumerate(plan["order"], 1):
            out.append(f"  {n}. " + " then ".join(names[x] for x in unit))
        if plan["cautions"]:
            out += ["", "Cautions that approving acknowledges:"]
            out += [f"  [{c['code']}] {'whole fleet' if c['scope'] == 'fleet' else names.get(c['scope'], c['scope'])}: {c['text']}" for c in plan["cautions"]]
        if bundle.previous:
            out += ["", ("Continues the earlier run " if self.mode == "resume" else "Starts a NEW run, leaving the earlier one ") + f"{bundle.previous[0]} ({bundle.previous[1]})."]
        return out

    def approver(self):
        bundle = self.bundle
        plan, serials, acks = bundle.plan, [d["serial"] for d in bundle.ready()], bundle.acks()
        return lambda journal_path: cli._new_approval(plan["hash"], serials, acks, journal_path, "typed APPROVE on the upgrade screen")

    def summary(self):
        s = self.bundle.plan["summary"]
        line = (f"Plan read at {clock(self.bundle.created)} ({ago(time.time() - self.bundle.created)}), {self.bundle.label}: {s['ready']} ready, "
                f"{s['caution']} need acknowledgement, {s['blocked']} blocked, {s['skipped']} not planned. Nothing has been changed yet.")
        return ("ok" if self.tasks() else "error"), line + ("" if self.tasks() else " Nothing is ready to upgrade.")

    def review_buttons(self):
        standalone = ("Standalone firewalls: included (each is OFFLINE while it reboots)" if self.standalone else "Standalone firewalls: left out")
        out = []
        if self.tasks():
            if self.bundle.previous:
                out += [("Approve and CONTINUE the earlier run...", lambda: self.begin_approval("resume")),
                        ("Approve and start a NEW run...", lambda: self.begin_approval("fresh"))]
            else:
                out += [("Approve and run...", self.begin_approval)]
            out += [("Rehearse (dry run)", self.rehearse)]
        return out + [(standalone, self.toggle_standalone), ("Read the plan again", self.read_plan), ("Home", None)]

    def status(self):
        if self.stage == "reading":
            return "warn", "reading the plan..."
        if self.stage == "review":
            s = self.bundle.plan["summary"]
            return ("warn" if self.tasks() else "error"), (f"plan ready: {s['ready'] + s['caution']} to upgrade, {s['blocked']} blocked - waiting for your approval"
                                                           if self.tasks() else f"plan ready: nothing is ready ({s['blocked']} blocked)")
        if self.stage == "approving":
            return "warn", "waiting for you to type APPROVE"
        if self.stage == "running":
            return "warn", "RUNNING - upgrading firewalls now"
        if self.stage == "done":
            return ("ok" if not self.result else "error"), f"finished at {clock(self.finished, False)}" + ("" if not self.result else f" with an error (code {self.result})")
        return "dim", "not started - open it, then press Start"


def job_lines(job, width, recent=0):
    """One job as it appears in a list: what it is and how it stands, then (``recent`` > 0) the last lines of its log, else one line on
    what it is doing or how it ended."""
    if job.running:
        head, state = [("warn", ">> "), ("bold", job.title)], [("dim", f"    running {job.elapsed()}s")]
        tail = [("dim", f"    {job.elapsed()}s  {job.words()}")]
    elif job.failed:
        head, state = [("error", "!! "), ("bold", job.title)], [("error", f"    failed {clock(job.began, False)}, {job.elapsed()}s")]
        tail = [("error", f"    {clock(job.began, False)}  {job.latest()}")]
    else:
        head, state = [("ok", "ok "), ("bold", job.title)], [("dim", f"    finished {clock(job.began, False)}, took {job.elapsed()}s")]
        tail = [("dim", f"    {clock(job.began, False)}  took {job.elapsed()}s")]
    if not recent:
        return [cut(head, width), cut(tail, width)]
    lines = job.recent(recent)
    return [cut(head, width), cut(state, width)] + [cut([("dim", "      " + line)], width) for line in lines]


class JobsScreen(Screen):
    """Every piece of background work: the list on the left, the whole log of the chosen one on the right.

    Tab (or Right) moves into the log, where Up / Down / PgUp / PgDn / Home / End scroll all of it; Tab (or Left) goes back to the list.
    The log follows the newest line until you scroll up, and follows again when you reach the end."""
    name, title = "jobs", "background jobs"
    LIST = 46

    def __init__(self, shell):
        super().__init__(shell)
        self.chosen, self.offset, self.focus = None, None, "list"          # offset None: following the end of the log

    def ordered(self):
        return list(reversed(self.shell.jobs))               # newest first

    def current(self):
        jobs = self.ordered()
        return self.chosen if self.chosen in jobs else (jobs[0] if jobs else None)

    def enter(self):
        self.chosen, self.offset, self.focus = None, None, "list"

    def tick(self):
        return bool(self.shell.running_jobs())

    def render(self, width, height):
        jobs, current = self.ordered(), self.current()
        hint = (" Up / Down choose a job   Tab or Right: into its log   Esc home" if self.focus == "list"
                else " Up / Down / PgUp / PgDn / Home / End scroll the log   Tab or Left: back to the list   Esc home")
        top = [[("bold", " panupgrade - background jobs")], [("dim", hint)], []]
        room = max(height - len(top) - 1, 3)
        listing = []
        for job in jobs:
            rows = job_lines(job, self.LIST - 3)
            mark = ">" if job is current else " "
            on = job is current and self.focus == "list"
            listing += [[("focus" if on else "bold", f" {mark}")] + rows[0], [("", "  ")] + rows[1]]
        if not jobs:
            listing = [[("dim", " nothing has run yet: reading the fleet or the firewall list shows up here")]]
        narrow = width < 100
        detail = self._detail(current, width - 2 if narrow else max(width - self.LIST - 2, 20), room // 2 if narrow else room)
        if narrow:                                           # too narrow for two columns: the list, then the log
            body = (listing[:max(room // 2, 2)] + [[]] + detail)[:room]
        else:
            body = self._beside(listing[:room], detail, self.LIST, room)
        return [cut(line, width - 1) for line in (top + body)[:height]]

    def _detail(self, job, width, room):
        if job is None:
            return []
        state = "running" if job.running else ("failed" if job.failed else "finished")
        on = self.focus == "log"
        out = [[("focus" if on else "bold", f" {'>' if on else ' '} {job.title}")],
               [("dim", f"   started {clock(job.began)}   {state}   {job.elapsed()}s")], []]
        lines = job.log() or ["(nothing printed yet)" if job.running else "(it printed nothing)"]
        space = max(room - len(out) - 1, 2)
        end = max(len(lines) - space, 0)
        start = end if self.offset is None else max(0, min(self.offset, end))
        out += [[("", " " + line)] for line in lines[start:start + space]]
        where = "the end (following)" if start >= end and job.running else "the end" if start >= end else f"lines {start + 1}-{min(start + space, len(lines))}"
        out.append([("dim", f" {where} of {len(lines)}")] if len(lines) > space else [])
        return out

    @staticmethod
    def _beside(left, right, left_width, room):
        rows = []
        for n in range(room):
            a = left[n] if n < len(left) else []
            b = right[n] if n < len(right) else []
            rows.append(a + [("", " " * max(left_width - width_of(a), 0)), ("dim", "| ")] + b)
        return rows

    def _scroll(self, key):
        """Move the log. The current position is worked out from what was last drawn, so a followed log scrolls up from its end."""
        job = self.current()
        total = len(job.log()) if job else 0
        end = max(total - self.visible, 0)
        at = end if self.offset is None else min(self.offset, end)
        at = {"up": at - 1, "down": at + 1, "pgup": at - self.visible, "pgdn": at + self.visible, "home": 0, "end": end}[key]
        at = max(0, at)
        self.offset = None if at >= end else at

    visible = 10                                              # lines the log shows; set while drawing is not needed for the keys' sake

    def press(self, key):
        jobs, current = self.ordered(), self.current()
        at = jobs.index(current) if current in jobs else 0
        if key in ("tab", "right") and self.focus == "list" and jobs:
            self.focus = "log"
        elif key in ("tab", "left", "shift-tab") and self.focus == "log":
            self.focus = "list"
        elif key in ("esc", "q", "Q"):
            return "home"
        elif self.focus == "list":
            if key == "up" and jobs:
                self.chosen, self.offset = jobs[max(at - 1, 0)], None
            elif key == "down" and jobs:
                self.chosen, self.offset = jobs[min(at + 1, len(jobs) - 1)], None
            elif key == "enter":
                self.focus = "log"
            elif key in ("pgup", "pgdn", "home", "end"):
                self._scroll(key)                            # the log can still be scrolled without leaving the list
        elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
            self._scroll(key)
        elif key == "enter":
            self.focus = "list"
        return None


class HomeScreen(Screen):
    name, title = "home", "upgrade Palo Alto firewalls through Panorama, safely"
    TEXT = {1: "address", 2: "target"}                                   # the rows of the top block that are typed into
    TOP = 4                                                              # profile, address, target, the list a firewall offers

    def __init__(self, shell):
        super().__init__(shell)
        self.at = 0

    def stops(self):
        return self.TOP + len(STEP_ORDER) + 1                            # the top block, the steps, Quit

    def press(self, key):
        session = self.shell.session
        if key == "esc":
            return "quit"
        if key in ("down", "tab"):
            self.at = (self.at + 1) % self.stops()
        elif key in ("up", "shift-tab"):
            self.at = (self.at - 1) % self.stops()
        elif self.at == 0:
            if key in ("left", "right"):
                self.shell.switch_profile(1 if key == "right" else -1)
            elif key in ("enter", " "):
                return ("goto", "auth")                                  # manage profiles, or make the first one
        elif self.at in self.TEXT:
            attr = self.TEXT[self.at]
            text = getattr(session, attr)
            if key == "enter":
                self.at += 1
            elif key == "backspace":
                setattr(session, attr, text[:-1])
            elif key == "ctrl-u":
                setattr(session, attr, "")
            elif len(key) == 1 and key.isprintable():
                setattr(session, attr, text + key)
        elif self.at == 3:
            if key in ("enter", " "):
                return ("goto", "targets")                               # pick the target from what a firewall lists
        elif key == "enter" or key == " ":
            step = self.at - self.TOP
            return "quit" if step == len(STEP_ORDER) else ("goto", STEP_ORDER[step])
        elif key in ("q", "Q"):
            return "quit"
        return None

    def render(self, width, height):
        session = self.shell.session
        names = profiles.names()
        lines = [[("bold", " panupgrade - upgrade Palo Alto firewalls through Panorama, safely")], []]
        on = self.at == 0
        if session.profile:
            hint = f"  {names.index(session.profile.name) + 1} of {len(names)}   Left / Right switch; Enter manages profiles" if session.profile.name in names else ""
            lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Panorama profile  "), ("focus" if on else "", f"< {session.profile.name} >"), ("dim", hint)])
        else:
            lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Panorama profile  "), ("focus" if on else "", "< none >"),
                          ("dim", "  Enter makes one (a profile keeps one Panorama's accounts, list and records apart)")])
        on = self.at == 1
        lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Panorama address  "), ("focus" if on else "", f"[{session.address}{'_' if on else ''}]".ljust(28)),
                      ("dim", "  name or IP address")])
        on = self.at == 2
        lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Target PAN-OS      "), ("focus" if on else "", f"[{session.target}{'_' if on else ''}]".ljust(28)),
                      ("dim", "  needed to plan, prepare and upgrade")])
        on = self.at == 3
        lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Or choose it from the versions a firewall lists"), ("dim", "  Enter")])
        lines += [[], [("bold", " Steps in order"), ("dim", "   (Enter opens one; the Alt keys below jump from anywhere)")]]
        for n, name in enumerate(STEP_ORDER):
            here = self.at == self.TOP + n
            style, text = self.shell.status(name)
            label = self.shell.titles[name]
            lines.append([("focus" if here else "bold", f" {'>' if here else ' '} {n + 1}  {label.ljust(20)}"), ("focus" if here else style, " " + text)])
        here = self.at == self.stops() - 1
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Q  Quit")])
        return [cut(line, width - 1) for line in lines[:height]]


# ---- the shell ---------------------------------------------------------------------------------------------------------------

class Shell:
    """Holds the screens, the shared session, the hotkeys, the footer and every background job. It is the one 'form' the terminal loop runs."""
    titles = {"auth": "Sign in", "select": "Choose firewalls", "report": "Fleet report", "plan": "Plan the upgrade",
              "prepare": "Prepare", "upgrade": "Upgrade", "settings": "Settings", "releases": "PAN-OS releases", "jobs": "Jobs"}

    def __init__(self, session, run_command=None, read_devices=None, prepare_review=None, prepare_execute=None,
                 upgrade_review=None, upgrade_execute=None, read_versions=None):
        self.session = session
        self.run_command = run_command or self._run_command
        self.read_devices = read_devices or self._read_devices
        self.read_versions = read_versions or self._read_versions
        self.prepare_review = prepare_review or self._prepare_review
        self.prepare_execute = prepare_execute or self._prepare_execute
        self.upgrade_review = upgrade_review or self._upgrade_review
        self.upgrade_execute = upgrade_execute or self._upgrade_execute
        self.message, self.note, self._armed, self.after_sign_in = "", "", None, None
        self._quit_armed, self._quit_key, self._pressed = False, "", ""
        self.jobs = []
        self.screens = {
            "home": HomeScreen(self),
            "auth": FormScreen(self, "auth", "sign in", lambda: cli._auth_form(self._auth_args())),
            "select": SelectScreen(self),
            "report": JobScreen(self, "report", "fleet report", "report", intro="What state is the fleet in right now? Nothing is changed."),
            "plan": JobScreen(self, "plan", "plan the upgrade", "plan", needs_target=True,
                              intro="What would an upgrade do to each firewall, and why? Read-only: nothing is changed."),
            "prepare": PrepareScreen(self),
            "upgrade": UpgradeScreen(self),
            "targets": TargetScreen(self),
            "settings": FormScreen(self, "settings", "settings", self._settings_form),
            "releases": JobScreen(self, "releases", "PAN-OS releases", "releases", needs_address=False, auto=True,
                                  intro="The list of PAN-OS releases used to count minor versions."),
            "jobs": JobsScreen(self),
        }
        self.screen = self.screens["home"]

    # -- background work
    def start_job(self, title, fn, done=None, changes=False):
        """Run ``fn`` in the background. ``done(job)`` is called on the screen's thread once it has finished, whichever screen is in front.
        ``changes`` marks work that alters real firewalls, which quitting must not cut off without asking."""
        job = Job(fn, title, done, changes)
        self.jobs.append(job)
        finished = [j for j in self.jobs if not j.running]
        for old in finished[:-KEPT_JOBS]:
            self.jobs.remove(old)
        return job

    def running_jobs(self):
        return [job for job in self.jobs if job.running]

    # -- terminal loop interface
    @property
    def tick_s(self):
        if any(job.running or not job.reported for job in self.jobs):
            return 0.25
        return self.screen.tick_s

    def tick(self):
        changed = False
        for job in list(self.jobs):
            if not job.running and not job.reported:
                job.reported, changed = True, True
                if job.done is not None:
                    try:
                        job.done(job)
                    except Exception as exc:                  # a bug in handling an answer must not end the program
                        self.message = f"internal error handling {job.title}: {type(exc).__name__}: {exc}"
        if self.screen.tick():
            changed = True
        return changed or bool(self.running_jobs())

    def render(self, width, height, title=""):
        room = max(height - 1, 3)
        pane = min(52, width // 3 + 4) if width >= self.screen.pane_from and self.jobs and self.screen.name != "jobs" else 0
        if pane:
            left = self.screen.render(width - pane - 1, room)
            right = self._pane(pane - 2, room)
            body = [(left[n] if n < len(left) else []) for n in range(room)]
            body = [row + [("", " " * max(width - pane - 1 - width_of(row), 0)), ("dim", "| ")] + (right[n] if n < len(right) else [])
                    for n, row in enumerate(body)]
        else:
            body = self.screen.render(width, room)
        body = (body + [[]] * (height - 1))[:height - 1]
        hints = "  ".join(h for _k, _n, h in HOTKEYS)
        running = len(self.running_jobs())
        extra = f"{running} job{'s' if running != 1 else ''} running   " if running and not pane and self.screen.name != "jobs" else ""
        footer = [("dim", " " + (self.message + "   " if self.message else "") + (self.note + "   " if self.note and not self.message else "") + extra
                   + hints + f"   Alt-1..{len(STEP_ORDER)} steps")]
        return body + [cut(footer, width - 1)]

    def _pane(self, width, room):
        """The side pane: what is running and what has finished, newest first."""
        lines = [[("bold", "Background jobs"), ("dim", "   Alt-J for more")], []]
        for n, job in enumerate(reversed(self.jobs)):
            lines += job_lines(job, width, recent=RECENT if job.running or n == 0 else 0) + [[]]
        return [cut(line, width) for line in lines[:room]]

    def changing(self):
        """Background work that is altering real firewalls right now."""
        return [job for job in self.jobs if job.running and job.changes]

    def _may_leave(self, how):
        """The way out from anywhere, but not by accident while firewalls are being changed: ask once, then do it on the same key again."""
        if not self.changing():
            return True
        if self._quit_armed == how:
            return True
        self._quit_armed, self._quit_key = how, self._pressed
        what = " and ".join(job.title.split(" (")[0] for job in self.changing())
        self.message = (f"{what} is changing firewalls. Quitting now leaves a step unfinished (it is recorded; --resume continues it). "
                        "Press the same key again to quit anyway.")
        return False

    def press(self, key):
        self.message = ""
        self._pressed = key
        if key != self._quit_key:
            self._quit_armed = False                         # any other key withdraws the question
        if key == "ctrl-c":                                  # the way out from anywhere, even from a screen with unsaved changes
            return "cancel" if self._may_leave("ctrl-c") else None
        if key.startswith("alt-"):
            target = self._hotkey_target(key)
            return self._jump(target) if target else None
        self._armed = None
        result = self.screen.press(key)
        return self._navigate(result)

    # -- navigation
    def number(self, name):
        return STEP_ORDER.index(name) + 1 if name in STEP_ORDER else "?"

    def _hotkey_target(self, key):
        for hot, name, _label in HOTKEYS:
            if key == hot:
                return name
        if key[4:].isdigit() and 1 <= int(key[4:]) <= len(STEP_ORDER):
            return STEP_ORDER[int(key[4:]) - 1]
        return None

    def _jump(self, name):
        if self.screen.dirty() and self._armed != name:
            self._armed = name
            self.message = "This screen has unsaved changes: press the same key again to leave and lose them."
            return None
        self._armed = None
        return self.goto(name)

    def _navigate(self, result):
        if result == "quit":
            return "accept" if self._may_leave("quit") else None
        if result == "home":
            return self.goto("home")
        if isinstance(result, tuple) and result[0] == "goto":
            return self.goto(result[1])
        return None

    def goto(self, name):
        self.session.persist()
        self.screen = self.screens[name]
        self.note = ""
        self.screen.enter()
        return None

    def switch_profile(self, step):
        """Left / Right on the home screen: the next or previous Panorama profile. What each profile last read stays with it."""
        names = profiles.names()
        if not names:
            return
        at = names.index(self.session.profile.name) if self.session.profile and self.session.profile.name in names else -1
        self.session.use(names[(at + step) % len(names)])
        for screen in self.screens.values():
            screen.profile_changed()
        self.message = f"Now working in the profile {self.session.profile.name}."

    def form_finished(self, screen, result):
        """A form ended: accepted (saved) or cancelled. Back to where the person came from, or home."""
        saved = result == "accept" or bool(getattr(screen.form, "ever_saved", False))
        if screen.name == "select":
            screen.form = None
        back = None
        if screen.name == "auth":
            back, self.after_sign_in = (self.after_sign_in if saved else None), None
            if back:
                self.screens[back].reset()                      # the sign-in changed, so what it read before is stale
                self.screens[back].retry = True                 # and the read the person had started is tried again
        self.message = ("Saved." if saved else "")
        return ("goto", back) if back else "home"

    def need_sign_in(self, screen, problem):
        """A step could not sign in: take the person to the sign-in screen, then back to the step."""
        self.after_sign_in = screen.name
        self.goto("auth")
        self.screens["auth"].form.message = "!" + problem if self.screens["auth"].form else ""

    def need_certificate(self, screen, problem):
        """A step met a certificate it cannot verify: ask, on the sign-in screen, whether to trust it, then go back and read again."""
        self.after_sign_in = screen.name
        self.goto("auth")
        form = self.screens["auth"].form
        if form is not None:
            form.exit_after_trust = True
            form.ask_about_certificate(problem, lambda: None)

    # -- what each step can say about itself without touching the network
    def status(self, name):
        session = self.session
        try:
            return getattr(self, f"_status_{name}")(session)
        except Exception as exc:                          # a status line must never take the screen down
            return "error", f"({type(exc).__name__})"

    def _status_auth(self, session):
        if session.profile is None:
            return "warn", "no Panorama profile yet - open this to make one"
        try:
            accounts = credstore.read(session.creds_path())
        except credstore.NeedsSignIn:
            return "warn", f"{session.profile.name}: no credentials yet - open this to add an account"
        except Exception:
            return "error", "the credentials file cannot be read"
        if not accounts:
            return "warn", f"{session.profile.name}: no accounts yet - open this to add one"
        reading = site.account(session.profile)[0] or (next(iter(accounts)) if len(accounts) == 1 else "")
        if not reading:
            return "warn", f"{session.profile.name}: {len(accounts)} accounts, none chosen - open this to choose"
        changes = site.work_account(session.profile)[0]
        text = f"{reading} ({credstore.kind(accounts.get(reading))}) reads" + (f"; {changes} changes" if changes else "; changes use the same")
        test = profiles.sign_ins(session.profile.name).get(reading)
        if test:
            text += f"; last test {'worked' if test.get('ok') else 'FAILED'} {str(test.get('at', ''))[:16].replace('T', ' ')}"
        return ("ok" if not test or test.get("ok") else "warn"), text

    def _status_report(self, session):
        screen = self.screens["report"]
        record, job = screen.record(session), screen.reading(session)
        if record is None:
            return ("warn", "reading now...") if job else ("dim", "not read yet - open it, then press Start")
        hit = re.search(r"Needs attention \((\d+) firewall", record.text)
        text = f"read at {clock(record.when, False)} ({ago(time.time() - record.when)})" + (": reading again..." if job else "")
        return ("warn" if hit else "ok"), text + (f": {hit.group(1)} firewalls need attention" if hit else ": nothing needs attention")

    def _status_select(self, session):
        path = session.ws(cli.DEFAULT_LIST)
        if not path.is_file():
            return "dim", f"no {cli.DEFAULT_LIST} yet - open this to choose firewalls"
        return "ok", f"{len(cli._read_list(path))} firewalls in {cli.DEFAULT_LIST}"

    def _status_plan(self, session):
        screen = self.screens["plan"]
        record, job = screen.record(session), screen.reading(session)
        if not session.target.strip():
            return "dim", "needs a target PAN-OS version"
        if record is not None:
            return "ok", f"last run {clock(record.when, False)} for {session.target}" + (" (running again...)" if job else "")
        return ("warn", f"running for {session.target}...") if job else ("dim", f"not run yet for {session.target} - open it, then press Start")

    def _status_prepare(self, session):
        return self.screens["prepare"].status()

    def _status_upgrade(self, session):
        return self.screens["upgrade"].status()

    def _status_settings(self, session):
        path = session.ws(cli.DEFAULT_SETTINGS)
        return ("ok", f"{cli._tilde(path.resolve())} in use") if path.is_file() else ("dim", "built-in defaults (no settings file yet)")

    def _status_releases(self, session):
        return "dim", "the table used to count minor versions"

    def _status_jobs(self, session):
        running, failed = len(self.running_jobs()), len([j for j in self.jobs if j.failed])
        if not self.jobs:
            return "dim", "nothing has run yet"
        text = f"{running} running, {len(self.jobs) - running} finished" + (f", {failed} failed" if failed else "")
        return ("warn" if running or failed else "ok"), text

    # -- pieces the screens call
    def _auth_args(self):
        return self.session.args_for("auth")

    def _settings_form(self):
        args = self.session.args_for("settings", address=False)
        try:
            return cli._settings_form(args), ""
        except Exception as exc:
            return None, f"cannot open the settings: {exc}"

    @staticmethod
    def _run_command(args):
        return args.func(args)

    @staticmethod
    def _prepare_review(args):
        """Read the fleet and build the prepare plan (changes nothing). A PrepareBundle, or an exit code when it failed."""
        style = cli._style(args)
        return cli._guarded(style, lambda: cli._prepare_review(args, style))

    @staticmethod
    def _upgrade_review(args):
        """Read the fleet and build the upgrade plan, printing it as `plan` does (changes nothing). An UpgradeBundle, or an exit code."""
        style = cli._style(args)
        return cli._guarded(style, lambda: cli._upgrade_review(args, style, show=True))

    @staticmethod
    def _upgrade_execute(args, bundle, approve):
        """Carry out the plan that was read and approved. An exit code."""
        style = cli._style(args)
        return cli._guarded(style, lambda: cli._upgrade_execute(args, style, bundle, approve))

    @staticmethod
    def _prepare_execute(args, bundle, approve):
        """Carry out the plan that was read and approved. An exit code."""
        style = cli._style(args)
        return cli._guarded(style, lambda: cli._prepare_execute(args, style, bundle, approve))

    @staticmethod
    def _read_devices(args):
        return cli._source(args).list_devices()

    @staticmethod
    def _read_versions(args, serial):
        return cli._source(args).available_versions(serial)
