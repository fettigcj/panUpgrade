# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""``panupgrade`` with no arguments: the whole workflow on one screen.

A home screen lists the steps in order (sign in, read the fleet, choose firewalls, plan, prepare, upgrade) with where each one
stands, and a few keys jump between them from anywhere: Alt-H home, Alt-A sign in, Alt-R report, Alt-F firewalls, Alt-P plan,
Alt-S settings, or Alt-1 to Alt-8 for the steps in order. Each step is the same code the command of that name runs, so the screen
and the command line cannot disagree: long work (reading Panorama) runs in the background while the screen stays alive, and its
output is shown as it would be printed.

Screens are small objects with ``render(width, height)``, ``press(key)`` (returning None, 'home', 'quit' or ('goto', name)),
``enter()`` and, where there is background work, ``tick()``. The terminal loop is in ``tui``; nothing here touches the terminal.
"""

import io
import re
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from . import cli, credstore, site
from .sources import SourceError

SPIN = "|/-\\"
HOTKEYS = [("alt-h", "home", "Alt-H home"), ("alt-a", "auth", "Alt-A sign in"), ("alt-r", "report", "Alt-R report"),
           ("alt-f", "select", "Alt-F firewalls"), ("alt-p", "plan", "Alt-P plan"), ("alt-s", "settings", "Alt-S settings")]
STEP_ORDER = ["auth", "report", "select", "plan", "prepare", "upgrade", "settings", "releases"]       # Alt-1 .. Alt-8


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


# ---- background work ---------------------------------------------------------------------------------------------------------

class Job:
    """A function run in a thread with everything it prints captured. The screen polls it; it never touches the terminal."""

    def __init__(self, fn):
        self.started, self.finished, self.text, self.code, self.value = time.monotonic(), None, "", None, None
        self._fn = fn
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                self.value = self._fn()
            self.code = self.value if isinstance(self.value, int) else 0
        except (SourceError, OSError, ValueError) as exc:    # the kind of trouble a person can act on
            self.code = 2
            err.write(f"error: {exc}\n")
        except BaseException as exc:                     # a bug must show up on the screen, not kill the program
            self.code = 2
            err.write(f"internal error: {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3))
        self.text = out.getvalue() + (("\n" if out.getvalue() else "") + err.getvalue() if err.getvalue() else "")
        self.finished = time.monotonic()

    @property
    def running(self):
        return self.finished is None

    def elapsed(self):
        return int((self.finished or time.monotonic()) - self.started)


# ---- session -----------------------------------------------------------------------------------------------------------------

class Session:
    """What the person has told the shell so far, shared by every screen."""

    def __init__(self, base, address="", target=""):
        self.base, self.address, self.target = base, address, target
        self.results = {}                                # screen name -> (time, text)

    def args_for(self, command, *extra, address=True):
        argv = [command] + (["--panorama", self.address.strip()] if address else []) + ["--color", "never", "--ascii", *extra]
        if getattr(self.base, "creds_file", None):
            argv += ["--creds-file", self.base.creds_file]
        args = cli.parse_args(argv)
        args._in_shell = True
        return args

    def base_args(self):
        """Arguments for questions that need no Panorama (where the credentials are, which settings file)."""
        return cli.parse_args(["auth"] + (["--creds-file", self.base.creds_file] if getattr(self.base, "creds_file", None) else []))


# ---- screens ---------------------------------------------------------------------------------------------------------------------

class Screen:
    name, title, tick_s = "", "", None

    def __init__(self, shell):
        self.shell = shell

    def enter(self):
        pass

    def reset(self):
        """Forget what was read, so the next visit reads again."""

    def tick(self):
        return False

    def dirty(self):
        return False


class FormScreen(Screen):
    """One of the existing forms (sign-in, settings) shown full size; it ends when the form does."""

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


class JobScreen(Screen):
    """A step that reads Panorama and shows what the matching command prints: the fleet report, the plan."""

    def __init__(self, shell, name, title, command, needs_target=False, intro="", needs_address=True):
        super().__init__(shell)
        self.name, self.title, self.command, self.needs_target, self.intro = name, title, command, needs_target, intro
        self.needs_address = needs_address
        self.job, self.lines, self.offset, self.bar, self.note = None, [], 0, 0, ""
        self.for_address = None

    @property
    def tick_s(self):
        return 0.25 if self.job is not None and self.job.running else None

    def reset(self):
        self.job, self.lines, self.for_address = None, [], None

    def enter(self):
        session = self.shell.session
        if self.job is None and not self.lines or self.for_address != (session.address, session.target):
            self.start()

    def start(self):
        session = self.shell.session
        self.lines, self.offset, self.note = [], 0, ""
        if self.needs_address and not session.address.strip():
            self.note = "Type the Panorama address on the home screen first (Alt-H)."
            return
        if self.needs_target and not session.target.strip():
            self.note = "Type the target PAN-OS version on the home screen first (Alt-H)."
            return
        self.for_address = (session.address, session.target)
        args = session.args_for(self.command, *(["--target", session.target.strip()] if self.needs_target else []), address=self.needs_address)
        self.args = args
        self.job = Job(lambda: self.shell.run_command(args))

    def tick(self):
        job = self.job
        if job is None:
            return False
        if job.running:
            return True
        if job.text is not None and not self.lines:
            self.lines = job.text.rstrip("\n").split("\n")
            self.shell.session.results[self.name] = (time.strftime("%H:%M"), job.text, job.code)
            if getattr(self.args, "_sign_in_problem", ""):
                self.shell.need_sign_in(self, self.args._sign_in_problem)
            return True
        return False

    def render(self, width, height):
        job = self.job
        lines = [[("bold", f" panupgrade - {self.title}")]]
        if self.intro:
            lines += wrapped("dim", self.intro, width)
        if self.note:
            lines += wrapped("error", self.note, width)
        elif job is not None and job.running:
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} working... {job.elapsed()}s (the screen stays live; Alt-keys still work)")])
        elif job is not None:
            ok = job.code == 0
            lines.append([("" if ok else "error", f" finished in {job.elapsed()}s" + ("" if ok else f" with an error (code {job.code})"))])
        lines.append([])
        bar = [("", " ")]
        for n, label in enumerate(("Refresh", "Change sign-in", "Home")):
            bar += [("focus" if self.bar == n else "bold", f"[ {label} ]"), ("", "  ")]
        room = max(height - len(lines) - 2, 3)
        self.offset = max(0, min(self.offset, max(len(self.lines) - room, 0)))
        body = [[("", " " + line)] for line in self.lines[self.offset:self.offset + room]]
        if len(self.lines) > room:
            lines += body + [[("dim", f" lines {self.offset + 1}-{min(self.offset + room, len(self.lines))} of {len(self.lines)}  (Up / Down / PgUp / PgDn scroll)")]]
        else:
            lines += body
        lines += [[]] * max(height - len(lines) - 1, 0)
        lines.append(bar)
        return [cut(line, width - 1) for line in lines[:height]]

    def press(self, key):
        page = 10
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
            self.offset = len(self.lines)
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
    def tick_s(self):
        return 0.25 if self.job is not None and self.job.running else None

    def dirty(self):
        return bool(self.form and self.form.dirty())

    def reset(self):
        self.job, self.form = None, None

    def enter(self):
        session = self.shell.session
        if self.form is not None and not self.form.dirty():
            self.form = None
        if self.form is not None:
            return
        if not session.address.strip():
            self.note = "Type the Panorama address on the home screen first (Alt-H)."
            return
        self.note = ""
        self.args = session.args_for("select")
        self.job = Job(lambda: self.shell.read_devices(self.args))

    def tick(self):
        job = self.job
        if job is None:
            return False
        if job.running:
            return True
        self.job = None
        if getattr(self.args, "_sign_in_problem", ""):
            self.shell.need_sign_in(self, self.args._sign_in_problem)
        elif job.code or not isinstance(job.value, list):
            self.note = (job.text or "could not read the firewall list").strip()
        else:
            self.form = cli._select_form(self.args, job.value)
        return True

    def render(self, width, height):
        if self.form is not None:
            return self.form.render(width, height, "panupgrade - choose the firewalls")
        lines = [[("bold", " panupgrade - choose the firewalls")], []]
        if self.job is not None:
            lines.append([("warn", f" {SPIN[int(time.monotonic() * 4) % 4]} reading the firewall list from Panorama... {self.job.elapsed()}s")])
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


class HomeScreen(Screen):
    name, title = "home", "upgrade Palo Alto firewalls through Panorama, safely"
    FIELDS = ("address", "target")

    def __init__(self, shell):
        super().__init__(shell)
        self.at = 0

    def stops(self):
        return len(self.FIELDS) + len(STEP_ORDER) + 1                    # the two fields, the steps, Quit

    def press(self, key):
        session = self.shell.session
        if key == "esc":
            return "quit"
        if key in ("down", "tab"):
            self.at = (self.at + 1) % self.stops()
        elif key in ("up", "shift-tab"):
            self.at = (self.at - 1) % self.stops()
        elif self.at < len(self.FIELDS):
            attr = self.FIELDS[self.at]
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
            step = self.at - len(self.FIELDS)
            return "quit" if step == len(STEP_ORDER) else ("goto", STEP_ORDER[step])
        elif key in ("q", "Q"):
            return "quit"
        return None

    def status(self, name):
        """(style, text) for one step: where it stands, without touching the network."""
        return self.shell.status(name)

    def render(self, width, height):
        session = self.shell.session
        lines = [[("bold", " panupgrade - upgrade Palo Alto firewalls through Panorama, safely")], []]
        on = self.at == 0
        lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Panorama address  "), ("focus" if on else "", f"[{session.address}{'_' if on else ''}]".ljust(28)),
                      ("dim", "  name or IP address")])
        on = self.at == 1
        lines.append([("focus" if on else "bold", f" {'>' if on else ' '} Target PAN-OS      "), ("focus" if on else "", f"[{session.target}{'_' if on else ''}]".ljust(28)),
                      ("dim", "  needed to plan, prepare and upgrade")])
        lines += [[], [("bold", " Steps in order"), ("dim", "   (Enter opens one; the Alt keys below jump from anywhere)")]]
        for n, name in enumerate(STEP_ORDER):
            here = self.at == len(self.FIELDS) + n
            style, text = self.status(name)
            label = self.shell.titles[name]
            lines.append([("focus" if here else "bold", f" {'>' if here else ' '} {n + 1}  {label.ljust(20)}"), ("focus" if here else style, " " + text)])
        here = self.at == self.stops() - 1
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Q  Quit")])
        return [cut(line, width - 1) for line in lines[:height]]


# ---- the shell ---------------------------------------------------------------------------------------------------------------

class Shell:
    """Holds the screens, the shared session, the hotkeys and the footer. It is the one 'form' the terminal loop runs."""
    titles = {"auth": "Sign in", "report": "Fleet report", "select": "Choose firewalls", "plan": "Plan the upgrade",
              "prepare": "Prepare", "upgrade": "Upgrade", "settings": "Settings", "releases": "PAN-OS releases"}

    def __init__(self, session, run_command=None, read_devices=None):
        self.session = session
        self.run_command = run_command or self._run_command
        self.read_devices = read_devices or self._read_devices
        self.message, self._armed, self.after_sign_in = "", None, None
        self.screens = {
            "home": HomeScreen(self),
            "auth": FormScreen(self, "auth", "sign in", lambda: cli._auth_form(self._auth_args())),
            "report": JobScreen(self, "report", "fleet report", "report", intro="What state is the fleet in right now? Nothing is changed."),
            "select": SelectScreen(self),
            "plan": JobScreen(self, "plan", "plan the upgrade", "plan", needs_target=True,
                              intro="What would an upgrade do to each firewall, and why? Read-only: nothing is changed."),
            "prepare": StaticScreen(self, "prepare", "prepare", lambda: self._not_yet("prepare")),
            "upgrade": StaticScreen(self, "upgrade", "upgrade", lambda: self._not_yet("upgrade")),
            "settings": FormScreen(self, "settings", "settings", self._settings_form),
            "releases": JobScreen(self, "releases", "PAN-OS releases", "releases", needs_address=False,
                                  intro="The list of PAN-OS releases used to count minor versions."),
        }
        self.screen = self.screens["home"]

    # -- terminal loop interface
    @property
    def tick_s(self):
        return self.screen.tick_s

    def tick(self):
        return self.screen.tick()

    def render(self, width, height, title=""):
        body = self.screen.render(width, max(height - 1, 3))
        body = (body + [[]] * (height - 1))[:height - 1]
        hints = "  ".join(h for _k, _n, h in HOTKEYS)
        footer = [("dim", " " + (self.message + "   " if self.message else "") + hints + "   Alt-1..8 steps")]
        return body + [cut(footer, width - 1)]

    def press(self, key):
        self.message = ""
        if key == "ctrl-c":                                  # the way out from anywhere, even from a screen with unsaved changes
            return "cancel"
        if key.startswith("alt-"):
            target = self._hotkey_target(key)
            return self._jump(target) if target else None
        self._armed = None
        result = self.screen.press(key)
        return self._navigate(result)

    # -- navigation
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
            return "accept"
        if result == "home":
            return self.goto("home")
        if isinstance(result, tuple) and result[0] == "goto":
            return self.goto(result[1])
        return None

    def goto(self, name):
        self.screen = self.screens[name]
        self.screen.enter()
        return None

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

    # -- what each step can say about itself without touching the network
    def status(self, name):
        session = self.session
        try:
            return getattr(self, f"_status_{name}")(session)
        except Exception as exc:                          # a status line must never take the screen down
            return "error", f"({type(exc).__name__})"

    def _status_auth(self, session):
        path, _origin = cli._creds(session.base)
        try:
            accounts = credstore.read(path)
        except credstore.NeedsSignIn:
            return "warn", "no credentials yet - open this to add an account"
        except Exception:
            return "error", "the credentials file cannot be read"
        if not accounts:
            return "warn", "no accounts yet - open this to add one"
        reading = site.account()[0] or (next(iter(accounts)) if len(accounts) == 1 else "")
        if not reading:
            return "warn", f"{len(accounts)} accounts, none chosen - open this to choose"
        changes = site.work_account()[0]
        return "ok", f"{reading} ({credstore.kind(accounts.get(reading))}) reads" + (f"; {changes} changes" if changes else "; changes use the same")

    def _status_report(self, session):
        got = session.results.get("report")
        if not got:
            return "dim", "not read yet - needs the Panorama address"
        hit = re.search(r"Needs attention \((\d+) firewall", got[1])
        return ("warn" if hit else "ok"), f"read at {got[0]}" + (f": {hit.group(1)} firewalls need attention" if hit else ": nothing needs attention")

    def _status_select(self, session):
        path = Path(cli.DEFAULT_LIST)
        if not path.is_file():
            return "dim", f"no {cli.DEFAULT_LIST} yet - open this to choose firewalls"
        return "ok", f"{len(cli._read_list(path))} firewalls in {cli.DEFAULT_LIST}"

    def _status_plan(self, session):
        got = session.results.get("plan")
        if not session.target.strip():
            return "dim", "needs a target PAN-OS version"
        return ("ok", f"last run {got[0]} for {session.target}") if got else ("dim", f"not run yet for {session.target}")

    def _status_prepare(self, session):
        return "dim", "not on this screen yet: use the command line"

    def _status_upgrade(self, session):
        return "dim", "not on this screen yet: use the command line"

    def _status_settings(self, session):
        path = Path(cli.DEFAULT_SETTINGS)
        return ("ok", f"{cli._tilde(path.resolve())} in use") if path.is_file() else ("dim", "built-in defaults (no settings file yet)")

    def _status_releases(self, session):
        return "dim", "the table used to count minor versions"

    # -- pieces the screens call
    def _auth_args(self):
        return self.session.args_for("auth") if self.session.address.strip() else self.session.base_args()

    def _settings_form(self):
        args = self.session.base_args()
        args.settings, args.set = None, None
        try:
            return cli._settings_form(args), ""
        except Exception as exc:
            return None, f"cannot open the settings: {exc}"

    def _not_yet(self, step):
        address, target = self.session.address.strip() or "<panorama>", self.session.target.strip() or "<version>"
        what = "get the firewalls ready (content, config sync, image downloads)" if step == "prepare" else "run the upgrade"
        return (f"This step is not on the screen yet. Until it is, it runs from the command line, where you type APPROVE to confirm it.\n\n"
                f"To {what}:\n\n    panupgrade {step} --panorama {address} --target {target}\n\n"
                f"It uses {cli.DEFAULT_LIST} for the firewalls and the same sign-in and settings you have just set up.")

    @staticmethod
    def _run_command(args):
        return args.func(args)

    @staticmethod
    def _read_devices(args):
        return cli._source(args).list_devices()
