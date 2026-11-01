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
        self._out, self._err = io.StringIO(), io.StringIO()
        self._fn = fn
        threading.Thread(target=self._run, daemon=True).start()

    def _note(self, text):
        self.stage = text

    def _run(self):
        out, err = self._out, self._err
        _capture_start(out, err)
        progress.listen(self._note)
        try:
            self.value = self._fn()
            self.code = self.value if isinstance(self.value, int) else 0
        except (SourceError, OSError, ValueError) as exc:    # the kind of trouble a person can act on
            self.code = 2
            err.write(f"error: {exc}\n")
        except BaseException as exc:                     # a bug must show up on the screen, not kill the program
            self.code = 2
            err.write(f"internal error: {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3))
        finally:
            progress.listen(None)
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

    def __init__(self, shell, name, title, command, needs_target=False, intro="", needs_address=True):
        super().__init__(shell)
        self.name, self.title, self.command, self.needs_target, self.intro = name, title, command, needs_target, intro
        self.needs_address = needs_address
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
        if key not in self.records and key not in self.running:
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

    def _finished(self, job, args, key):
        if self.running.get(key) is not job:              # it was reset while this ran
            return
        del self.running[key]
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
        if job is not None:
            seen = f"   (the result from {clock(record.when)} stays on show until this finishes)" if record else ""
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} working... {job.elapsed()}s {job.latest()}"), ("dim", seen)])
        elif record is not None:
            ok = not record.code
            lines.append([("" if ok else "error", f" result from {clock(record.when)} ({ago(time.time() - record.when)}), finished in {record.seconds}s"
                           + ("" if ok else f" with an error (code {record.code})")), ("dim", "   R refreshes")])
        lines.append([])
        bar = [("", " ")]
        for n, label in enumerate(("Refresh", "Change sign-in", "Home")):
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
            self.start()
        elif key in ("left", "shift-tab"):
            self.bar = max(self.bar - 1, 0)
        elif key in ("right", "tab"):
            self.bar = min(self.bar + 1, 2)
        elif key == "enter":
            if self.bar == 0:
                self.start()
            elif self.bar == 1:
                self.shell.after_sign_in = self.name              # signing in again, then straight back here to read again
                return ("goto", "auth")
            else:
                return "home"
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
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} reading the firewall list from Panorama... {self.job.elapsed()}s {self.job.latest()}")])
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


class StaticScreen(Screen):
    """A page of plain text, for steps that are not on this screen yet."""

    def __init__(self, shell, name, title, make_text):
        super().__init__(shell)
        self.name, self.title, self._text = name, title, make_text

    def render(self, width, height):
        lines = [[("bold", f" panupgrade - {self.title}")], []]
        for paragraph in self._text().split("\n"):
            lines += wrapped("", paragraph, width) if paragraph.strip() else [[]]
        lines += [[]] * max(height - len(lines) - 1, 0)
        lines.append([("", " "), ("focus", "[ Home ]")])
        return [cut(line, width - 1) for line in lines[:height]]

    def press(self, key):
        return "home" if key in ("enter", "esc", " ") else None


class PrepareScreen(Screen):
    """Get the firewalls ready ahead of the window, in the same three steps the command takes: read the plan (changes nothing), approve it by
    typing APPROVE, then run it. Leaving the screen does not stop a run; the Jobs screen shows it and quitting asks first."""
    name, title = "prepare", "prepare"
    INTRO = ("Gets the firewalls ready ahead of the window: brings HA members' content level, forces a passive member's configuration to "
             "match its active, and downloads the images for the target. It changes the firewalls, so nothing starts until you have read the "
             "plan and typed APPROVE. It never installs PAN-OS, reboots or fails anything over.")

    def __init__(self, shell):
        super().__init__(shell)
        self.stage, self.job, self.bundle, self.args, self.key_ = "idle", None, None, None, None
        self.lines, self.offset, self.bar, self.note, self.typed = [], 0, 0, "", ""
        self.finished = None
        self.result = None

    # -- where it stands
    def key(self, session):
        return (session.profile.name if session.profile else "", session.address.strip(), session.target.strip())

    def busy(self):
        return self.stage in ("reading", "running")

    def reset(self):
        if self.busy():
            return                                       # never lose track of work that is going on
        self.stage, self.job, self.bundle, self.lines, self.note, self.typed = "idle", None, None, [], "", ""

    def profile_changed(self):
        self.reset()

    def enter(self):
        if self.pending:
            self.route_pending()
            return
        if self.stage in ("review", "approving") and self.key_ != self.key(self.shell.session):
            self.reset()                                 # the plan was for another address or target
        if self.stage == "idle":
            self.read_plan()

    def tick(self):
        return self.busy()

    # -- the three steps
    def _check(self):
        session = self.shell.session
        if not session.address.strip():
            return "Choose or make a Panorama profile, or type its address, on the home screen first (Alt-H)."
        if not session.target.strip():
            return "Type the target PAN-OS version on the home screen first (Alt-H): the images for it are what gets pre-downloaded."
        return ""

    def read_plan(self):
        if self.busy():
            return
        self.note = self._check()
        if self.note:
            return
        session = self.shell.session
        self.args = args = session.args_for("prepare", "--target", session.target.strip())
        self.key_ = self.key(session)
        self.lines, self.offset, self.bundle, self.typed = [], 0, None, ""
        self.stage = "reading"
        where = session.profile.name if session.profile else session.address.strip()
        self.job = self.shell.start_job(f"Prepare plan ({where})", lambda: self.shell.prepare_review(args), self._planned)

    def _planned(self, job):
        if self.job is not job or self.stage != "reading":
            return
        self.lines = job.text.rstrip("\n").split("\n") if job.text else []
        self.offset = 0
        if self.settle_problem(self.args):
            self.stage = "idle"
        elif job.code or not hasattr(job.value, "plan"):
            self.stage, self.bundle = "failed", None
        else:
            self.stage, self.bundle = "review", job.value
        self.bar = 0

    def begin_approval(self):
        age = time.time() - self.bundle.created
        if age > PLAN_MAX_AGE:
            self.note = f"The plan was read {ago(age)[:-4]} ago and the fleet may have changed. Read it again before approving."
            return
        self.note, self.typed, self.stage, self.offset = "", "", "approving", 0

    def run(self):
        """Typed APPROVE: carry out exactly the plan that was read."""
        bundle = self.bundle
        if time.time() - bundle.created > PLAN_MAX_AGE:
            self.stage, self.note = "review", "That plan is now too old to approve. Read it again."
            return
        plan, args, session = bundle.plan, self.args, self.shell.session

        def approve(journal_path):
            return cli._new_approval(plan["hash"], plan["serials"], [], journal_path, "typed APPROVE on the prepare screen")
        self.stage, self.note, self.offset, self.lines = "running", "", 0, []
        where = session.profile.name if session.profile else session.address.strip()
        self.job = self.shell.start_job(f"Prepare ({where})", lambda: self.shell.prepare_execute(args, bundle, approve), self._ran, changes=True)

    def rehearse(self):
        """A dry run: the same steps against a model of the fleet. Nothing is sent to any firewall and nothing is asked."""
        session = self.shell.session
        args = session.args_for("prepare", "--target", session.target.strip(), "--dry-run")
        self.args, self.stage, self.note, self.offset, self.lines = args, "running", "", 0, []
        where = session.profile.name if session.profile else session.address.strip()
        self.job = self.shell.start_job(f"Prepare rehearsal ({where})", lambda: self.shell.run_command(args), self._ran)

    def _ran(self, job):
        if self.job is not job or self.stage != "running":
            return
        self.stage, self.lines, self.offset, self.result, self.finished = "done", job.text.rstrip("\n").split("\n"), 0, job.code, time.time()
        self.bundle, self.bar = None, 0
        for name in ("report", "plan"):
            self.shell.screens[name].reset()               # the fleet has changed, so what was read before is out of date

    def execute_nothing_to_prepare(self):
        """Nothing needs preparing: go straight on to the upgrade review and kit, which change no firewall."""
        self.run()

    # -- drawing
    def buttons(self):
        if self.stage == "review":
            tasks = bool(self.bundle and self.bundle.plan["serials"])
            return [("Approve and run..." if tasks else "Continue to the upgrade kit", self.begin_approval if tasks else self.run),
                    ("Rehearse (dry run)", self.rehearse), ("Read the plan again", self.read_plan), ("Home", None)]
        if self.stage == "running":
            return [("Home (it keeps running)", None)]
        if self.stage in ("done", "failed", "idle"):
            return [("Read the plan again" if self.stage != "idle" else "Read the plan", self.read_plan), ("Home", None)]
        return [("Home", None)]

    def body(self):
        if self.stage == "approving":
            from . import prepare as prepare_mod
            plan = self.bundle.plan
            return [cli._prepare_heading(plan), ""] + [f"  {line}" for line in prepare_mod.describe(plan)]
        if self.stage == "running" and self.job is not None:
            text = self.job.live().rstrip("\n")
            return text.split("\n") if text else []
        return self.lines

    def render(self, width, height):
        lines = [[("bold", " panupgrade - prepare")]] + (wrapped("dim", self.INTRO, width) if height >= 24 else [])
        if self.note:
            lines += wrapped("error", self.note, width)
        spin = SPIN[int(time.monotonic() * 4) % 4]
        job = self.job
        if self.stage == "reading":
            lines.append([("warn", f" {spin} reading the fleet and building the plan... {job.elapsed()}s {job.latest()}")])
        elif self.stage == "review":
            tasks = len(self.bundle.plan["serials"])
            lines.append([("ok", f" Plan read at {clock(self.bundle.created)} ({ago(time.time() - self.bundle.created)}): "
                                 + (f"{tasks} firewall{'s' if tasks != 1 else ''} to prepare. Nothing has been changed yet." if tasks
                                    else "nothing to prepare. Nothing has been changed.")),
                          ("dim", f"   {self.bundle.label}")])
        elif self.stage == "approving":
            lines.append([("warn", " Nothing starts until you type APPROVE below. Esc goes back without doing anything.")])
        elif self.stage == "running":
            lines.append([("warn", f" {spin} {'preparing' if job.changes else 'rehearsing'}... {job.elapsed()}s {job.latest()}"),
                          ("dim", "   leaving this screen does not stop it; Alt-J shows it")])
        elif self.stage == "done":
            ok = not self.result
            lines.append([("ok" if ok else "error", f" finished at {clock(self.finished)} ({ago(time.time() - self.finished)})"
                           + ("" if ok else f" with an error (code {self.result}); read the output below"))])
        elif self.stage == "failed":
            lines.append([("error", " The plan could not be built; the reason is below.")])
        lines.append([])
        footer = []
        if self.stage == "approving":
            footer = [[("bold", " Type APPROVE to go ahead: "), ("focus", f"[{self.typed}_]".ljust(14)), ("dim", "   Enter confirms, Esc goes back")]]
        else:
            bar = [("", " ")]
            for n, (label, _action) in enumerate(self.buttons()):
                bar += [("focus" if self.bar == n else "bold", f"[ {label} ]"), ("", "  ")]
            footer = [bar]
        body_lines = self._wrap_all(self.body(), width - 3)
        lines = lines[:max(height - len(footer) - 4, 2)]
        room = max(height - len(lines) - len(footer) - 1, 3)
        self.offset = max(0, min(self.offset, max(len(body_lines) - room, 0)))
        shown = [[("", " " + line)] for line in body_lines[self.offset:self.offset + room]]
        if len(body_lines) > room:
            shown.append([("dim", f" lines {self.offset + 1}-{min(self.offset + room, len(body_lines))} of {len(body_lines)}  (Up / Down / PgUp / PgDn scroll)")])
        lines += shown
        lines = lines[:max(height - len(footer), 0)]
        lines += [[]] * max(height - len(lines) - len(footer), 0)
        lines += footer
        return [cut(line, width - 1) for line in lines[:height]]

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
            action = self.buttons()[min(self.bar, len(self.buttons()) - 1)][1]
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


def job_lines(job, width):
    """One job as it appears in a list: what it is and how it stands, then what it is doing or how it ended."""
    if job.running:
        head = [("warn", ">> "), ("bold", job.title)]
        tail = [("dim", f"    {job.elapsed()}s  {job.latest()}")]
    elif job.failed:
        head = [("error", "!! "), ("bold", job.title)]
        tail = [("error", f"    {clock(job.began, False)}  {job.latest()}")]
    else:
        head = [("ok", "ok "), ("bold", job.title)]
        tail = [("dim", f"    {clock(job.began, False)}  took {job.elapsed()}s")]
    return [cut(head, width), cut(tail, width)]


class JobsScreen(Screen):
    """Every piece of background work: the list on the left, what the chosen one printed on the right."""
    name, title = "jobs", "background jobs"
    LIST = 46

    def __init__(self, shell):
        super().__init__(shell)
        self.chosen, self.offset = None, 0

    def ordered(self):
        return list(reversed(self.shell.jobs))               # newest first

    def current(self):
        jobs = self.ordered()
        return self.chosen if self.chosen in jobs else (jobs[0] if jobs else None)

    def enter(self):
        self.chosen, self.offset = None, 0

    def tick(self):
        return bool(self.shell.running_jobs())

    def render(self, width, height):
        jobs, current = self.ordered(), self.current()
        top = [[("bold", " panupgrade - background jobs")],
               [("dim", " Up / Down choose a job   PgUp / PgDn scroll its output   Esc home")], []]
        room = max(height - len(top) - 1, 3)
        listing = []
        for job in jobs:
            rows = job_lines(job, self.LIST - 3)
            mark = ">" if job is current else " "
            listing += [[("focus" if job is current else "bold", f" {mark}")] + rows[0], [("", "  ")] + rows[1]]
        if not jobs:
            listing = [[("dim", " nothing has run yet: reading the fleet or the firewall list shows up here")]]
        detail = self._detail(current, max(width - self.LIST - 2, 20), room)
        if width < 100:                                      # too narrow for two columns: the list, then the output
            body = (listing[:max(room // 2, 2)] + [[]] + detail)[:room]
        else:
            body = self._beside(listing[:room], detail, self.LIST, room)
        return [cut(line, width - 1) for line in (top + body)[:height]]

    def _detail(self, job, width, room):
        if job is None:
            return []
        state = "running" if job.running else ("failed" if job.failed else "finished")
        out = [[("bold", f" {job.title}")],
               [("dim", f" started {clock(job.began)}   {state}   {job.elapsed()}s")], []]
        text = job.live().rstrip("\n")
        lines = text.split("\n") if text else ["(nothing printed yet)" if job.running else "(it printed nothing)"]
        if job.running and job.stage:
            lines = [f"now: {job.stage}", ""] + lines
        space = max(room - len(out) - 1, 2)
        self.offset = max(0, min(self.offset, max(len(lines) - space, 0)))
        out += [[("", " " + line)] for line in lines[self.offset:self.offset + space]]
        if len(lines) > space:
            out.append([("dim", f" lines {self.offset + 1}-{min(self.offset + space, len(lines))} of {len(lines)}")])
        return out

    @staticmethod
    def _beside(left, right, left_width, room):
        rows = []
        for n in range(room):
            a = left[n] if n < len(left) else []
            b = right[n] if n < len(right) else []
            rows.append(a + [("", " " * max(left_width - width_of(a), 0)), ("dim", "| ")] + b)
        return rows

    def press(self, key):
        jobs, current = self.ordered(), self.current()
        at = jobs.index(current) if current in jobs else 0
        if key == "up" and jobs:
            self.chosen, self.offset = jobs[max(at - 1, 0)], 0
        elif key == "down" and jobs:
            self.chosen, self.offset = jobs[min(at + 1, len(jobs) - 1)], 0
        elif key == "pgup":
            self.offset = max(self.offset - 10, 0)
        elif key == "pgdn":
            self.offset += 10
        elif key == "home":
            self.offset = 0
        elif key == "end":
            self.offset = 10 ** 6
        elif key in ("esc", "enter", "q", "Q"):
            return "home"
        return None


class HomeScreen(Screen):
    name, title = "home", "upgrade Palo Alto firewalls through Panorama, safely"
    TEXT = {1: "address", 2: "target"}                                   # the rows of the top block that are typed into
    TOP = 3                                                              # profile, address, target

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

    def __init__(self, session, run_command=None, read_devices=None, prepare_review=None, prepare_execute=None):
        self.session = session
        self.run_command = run_command or self._run_command
        self.read_devices = read_devices or self._read_devices
        self.prepare_review = prepare_review or self._prepare_review
        self.prepare_execute = prepare_execute or self._prepare_execute
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
            "upgrade": StaticScreen(self, "upgrade", "upgrade", lambda: self._not_yet("upgrade")),
            "settings": FormScreen(self, "settings", "settings", self._settings_form),
            "releases": JobScreen(self, "releases", "PAN-OS releases", "releases", needs_address=False,
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
        for job in reversed(self.jobs):
            lines += job_lines(job, width) + [[]]
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
        self.message = ("Prepare is changing firewalls. Quitting now leaves a step unfinished (it is recorded; --resume continues it). "
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
            return ("warn", "reading now...") if job else ("dim", "not read yet - needs the Panorama address")
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
        return ("warn", f"running for {session.target}...") if job else ("dim", f"not run yet for {session.target}")

    def _status_prepare(self, session):
        screen = self.screens["prepare"]
        if screen.stage == "reading":
            return "warn", "reading the plan..."
        if screen.stage == "review":
            tasks = len(screen.bundle.plan["serials"])
            return "warn", (f"plan ready: {tasks} firewall{'s' if tasks != 1 else ''} to prepare - waiting for your approval" if tasks
                            else "plan ready: nothing to prepare")
        if screen.stage == "approving":
            return "warn", "waiting for you to type APPROVE"
        if screen.stage == "running":
            return "warn", "RUNNING - changing firewalls now"
        if screen.stage == "done":
            return ("ok" if not screen.result else "error"), f"finished at {clock(screen.finished, False)}" + ("" if not screen.result else f" with an error (code {screen.result})")
        return "dim", "not started - open this to read the plan"

    def _status_upgrade(self, session):
        return "dim", "not on this screen yet: use the command line"

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

    def _not_yet(self, step):
        profile, target = self.session.profile, self.session.target.strip() or "<version>"
        where = f"--profile {profile.name}" if profile else f"--panorama {self.session.address.strip() or '<panorama>'}"
        what = "run the upgrade"
        return (f"This step is not on the screen yet. Until it is, it runs from the command line, where you type APPROVE to confirm it.\n\n"
                f"To {what}:\n\n    panupgrade {step} {where} --target {target}\n\n"
                f"It uses {cli.DEFAULT_LIST} for the firewalls and the same sign-in and settings you have just set up.")

    @staticmethod
    def _run_command(args):
        return args.func(args)

    @staticmethod
    def _prepare_review(args):
        """Read the fleet and build the prepare plan (changes nothing). A PrepareBundle, or an exit code when it failed."""
        style = cli._style(args)
        return cli._guarded_prepare(style, lambda: cli._prepare_review(args, style))

    @staticmethod
    def _prepare_execute(args, bundle, approve):
        """Carry out the plan that was read and approved. An exit code."""
        style = cli._style(args)
        return cli._guarded_prepare(style, lambda: cli._prepare_execute(args, style, bundle, approve))

    @staticmethod
    def _read_devices(args):
        return cli._source(args).list_devices()
