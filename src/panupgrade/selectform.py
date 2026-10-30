# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The fill-in form behind ``panupgrade select``: tab between the filter fields and watch the matching firewalls update.

``Form`` holds the state and knows nothing about terminals, so it is tested by pressing keys at it. ``show`` draws it
on a real terminal (standard library only: ``msvcrt`` on Windows, ``termios`` elsewhere).
"""

import os
import shutil
import sys

from .planner import Selection
from .versions import Version, VersionError

TEXT, LIST = "text", "list"

# attribute on Selection, label, kind, command-line flag, example shown while the field is focused
FIELDS = [
    ("name_glob", "Hostname pattern", TEXT, "--name", "e.g. site1-*   (* matches anything, ? matches one character)"),
    ("name_contains", "Hostname contains", TEXT, "--name-contains", "any part of the name, e.g. gate"),
    ("version_is", "Running PAN-OS", TEXT, "--version-is", "e.g. 10.2.1 or 10.2.*"),
    ("version_older_than", "Older than", TEXT, "--version-older-than", "e.g. 10.2.11   (running something below this)"),
    ("version_newer_than", "Newer than", TEXT, "--version-newer-than", "e.g. 10.2.2   (running something above this)"),
    ("ha_states", "HA state", LIST, "--ha-state", "e.g. active, passive"),
    ("models", "Model", LIST, "--model", "e.g. PA-VM"),
    ("device_groups", "Device group", LIST, "--device-group", "as named in Panorama"),
    ("tags", "Tag", LIST, "--tag", "as named in Panorama"),
    ("serials", "Serial numbers", LIST, "--serial", "separate with commas"),
]
SAVE = ("out", "Save the list to", TEXT, "--out", "a file name, e.g. fleet.txt   (leave empty to just look)")
LABEL_WIDTH = max(len(f[1]) for f in FIELDS + [SAVE])
VERSION_FIELDS = ("version_older_than", "version_newer_than")

HELP = ["Tab / Shift-Tab (or Down / Up): next / previous field      Left / Right: pick a value this fleet has",
        "Enter: done      Esc: cancel      Ctrl-U: clear the field"]


def _split(text):
    return [part for part in text.replace(",", " ").split() if part]


def _choices(refs):
    """The values that exist in this fleet, per field, so the form can offer them instead of making people guess."""
    def distinct(values):
        return sorted({v for v in values if v})

    def versions(values):
        def key(text):
            try:
                return (0, Version.parse(text))
            except VersionError:
                return (1, text)          # an unreadable version sorts last, as plain text
        return sorted({v for v in values if v}, key=key)
    return {"name_glob": [], "name_contains": [], "serials": [],
            "version_is": versions(r.sw_version for r in refs),
            "version_older_than": versions(r.sw_version for r in refs),
            "version_newer_than": versions(r.sw_version for r in refs),
            "ha_states": distinct((r.ha_state or "").lower() for r in refs),
            "models": distinct(r.model for r in refs),
            "device_groups": distinct(r.device_group for r in refs),
            "tags": distinct(t for r in refs for t in (r.tags or []))}


class Form:
    def __init__(self, refs, initial=None, out=""):
        self.refs = refs
        self.choices = _choices(refs)
        self.fields = FIELDS + [SAVE]
        self.values = {}
        initial = initial or Selection()
        for attr, _label, kind, _flag, _eg in FIELDS:
            value = getattr(initial, attr)
            self.values[attr] = ", ".join(value) if kind == LIST else (value or "")
        self.values["out"] = out or ""
        self.focus = 0

    # ---- what the fields mean
    def selection(self, base=None):
        """The Selection the fields describe. A version that does not parse is left out so the preview keeps working."""
        sel = Selection(accept_standalone_outage=getattr(base, "accept_standalone_outage", False))
        bad = self.errors()
        for attr, _label, kind, _flag, _eg in FIELDS:
            if attr not in bad:
                text = self.values[attr].strip()
                setattr(sel, attr, _split(text) if kind == LIST else text)
        return sel

    def errors(self):
        found = {}
        for attr in VERSION_FIELDS:
            text = self.values[attr].strip()
            if text:
                try:
                    Version.parse(text)
                except VersionError as exc:
                    found[attr] = str(exc)
        return found

    def matching(self):
        sel = self.selection()
        return [r for r in self.refs if sel.matches(r)]

    def flags(self):
        """The command-line flags that make the same selection, for printing after the form closes."""
        out = []
        for attr, _label, kind, flag, _eg in FIELDS + [SAVE]:
            text = self.values[attr].strip()
            if not text:
                continue
            if attr == "out" or kind == TEXT:
                out.append((flag, text))
            else:
                out += [(flag, item) for item in _split(text)]
        return out

    # ---- keys
    def press(self, key):
        """Apply one key. Returns 'accept' or 'cancel' when the form is finished, otherwise None."""
        attr = self.fields[self.focus][0]
        kind = self.fields[self.focus][2]
        text = self.values[attr]
        count = len(self.fields)
        if key in ("tab", "down"):
            self.focus = (self.focus + 1) % count
        elif key in ("shift-tab", "up"):
            self.focus = (self.focus - 1) % count
        elif key == "enter":
            return None if self.errors() else "accept"
        elif key in ("esc", "ctrl-c"):
            return "cancel"
        elif key == "backspace":
            self.values[attr] = text[:-1]
        elif key == "ctrl-u":
            self.values[attr] = ""
        elif key in ("right", "left"):
            self.values[attr] = self._pick(attr, kind, text, 1 if key == "right" else -1)
        elif len(key) == 1 and key.isprintable():
            self.values[attr] = text + key
        return None

    def _pick(self, attr, kind, text, step):
        known = self.choices.get(attr) or []
        if not known:
            return text
        if kind == LIST:
            have = _split(text)
            if step < 0:
                return ", ".join(have[:-1])
            remaining = [k for k in known if k not in have]
            return ", ".join(have + remaining[:1]) if remaining else text
        cycle = [""] + known
        at = cycle.index(text.strip()) if text.strip() in cycle else 0
        return cycle[(at + step) % len(cycle)]

    # ---- drawing
    def render(self, width, height, title=""):
        """The whole screen as a list of lines (no trailing newline), built to fit width x height."""
        errors = self.errors()
        matched = self.matching()
        lines = [("bold", f" {title}".rstrip() or " panupgrade select")] + [("dim", " " + h) for h in HELP] + [("", "")]
        for i, (attr, label, kind, _flag, example) in enumerate(self.fields):
            text = self.values[attr]
            room = max(12, min(40, width - LABEL_WIDTH - 8))
            shown = ("..." + text[-(room - 3):]) if len(text) > room else text
            cursor = "_" if i == self.focus else " "
            box = f"[{shown}{cursor}".ljust(room + 1) + "]"
            mark = ">" if i == self.focus else " "
            row = f" {mark} {label.ljust(LABEL_WIDTH)}  {box}"
            if attr in errors:
                lines.append(("error" if i != self.focus else "focus-error", f"{row}  {errors[attr]}"))
            else:
                lines.append(("focus" if i == self.focus else "", row))
        lines.append(("", ""))
        attr, _label, kind, _flag, example = self.fields[self.focus]
        known = self.choices.get(attr) or []
        if known:
            verb = "Right adds the next, Left removes the last" if kind == LIST else "Left / Right to step through them"
            lines.append(("dim", f" This fleet has: {', '.join(known)}   ({verb})"))
        else:
            lines.append(("dim", f" {example}"))
        lines += [("", ""), ("bold", f" {len(matched)} of {len(self.refs)} firewalls match")]
        room = height - len(lines) - 1
        if matched and room > 0:
            rows = [(r.hostname, r.serial, r.model, r.sw_version or "-", r.ha_state or "-", r.device_group or "-",
                     "" if r.connected else "OFFLINE") for r in matched]
            widths = [min(30, max(len(row[i]) for row in rows)) for i in range(7)]
            shown_rows = rows if len(rows) <= room else rows[:max(room - 1, 0)]
            for row in shown_rows:
                lines.append(("", "   " + "  ".join(row[i][:widths[i]].ljust(widths[i]) for i in range(7)).rstrip()))
            if len(shown_rows) < len(rows):
                lines.append(("dim", f"   ... and {len(rows) - len(shown_rows)} more"))
        return [(style, text[:max(width - 1, 1)]) for style, text in lines[:height]]


CODES = {"bold": "\x1b[1m", "dim": "\x1b[2m", "focus": "\x1b[7m", "error": "\x1b[31m", "focus-error": "\x1b[7;31m", "": ""}


def draw(form, out, width, height, title=""):
    parts = ["\x1b[H"]
    for style, text in form.render(width, height, title):
        parts.append(f"{CODES[style]}{text}\x1b[0m\x1b[K\r\n" if style else f"{text}\x1b[K\r\n")
    parts.append("\x1b[J")
    out.write("".join(parts))
    out.flush()


def run(form, read_key, out, size, title=""):
    """Show the form and feed it keys until it finishes. ``size()`` gives (width, height) and is re-read on every key so a
    resized window is followed. Returns True when accepted, False when cancelled."""
    while True:
        width, height = size()
        draw(form, out, width, height, title)
        result = form.press(read_key())
        if result:
            return result == "accept"


# ---- the real terminal ------------------------------------------------------------------------------------------------
_WIN_KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "\x0f": "shift-tab"}
_ANSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "Z": "shift-tab"}
_PLAIN_KEYS = {"\r": "enter", "\n": "enter", "\t": "tab", "\x7f": "backspace", "\x08": "backspace", "\x15": "ctrl-u", "\x03": "ctrl-c"}


def _read_key_windows():
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return _WIN_KEYS.get(msvcrt.getwch(), "")
    if ch == "\x1b":
        return "esc"
    return _PLAIN_KEYS.get(ch, ch)


def _read_key_posix(fd):
    import select
    first = os.read(fd, 1)
    if first == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        rest = os.read(fd, 1)
        if rest in (b"[", b"O"):
            while select.select([fd], [], [], 0.05)[0]:
                tail = os.read(fd, 1)
                if tail.isalpha() or tail == b"~":
                    return _ANSI_KEYS.get(tail.decode(), "")
        return ""
    ch = first
    if first[0] >= 0xC0:
        ch += os.read(fd, 1 if first[0] < 0xE0 else 2 if first[0] < 0xF0 else 3)
    ch = ch.decode("utf-8", "replace")
    return _PLAIN_KEYS.get(ch, ch)


def _enable_vt_on_windows():
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def show(form, title=""):
    """Run the form on the real terminal. Returns True when accepted, False when cancelled."""
    out = sys.stdout
    if os.name == "nt":
        _enable_vt_on_windows()
        read_key, restore = _read_key_windows, lambda: None
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        read_key = lambda: _read_key_posix(fd)   # noqa: E731
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)   # noqa: E731
    out.write("\x1b[?1049h\x1b[?25l")        # a separate screen, so the user's scrollback is left alone; hide the cursor
    try:
        return run(form, read_key, out, lambda: tuple(shutil.get_terminal_size((100, 30))), title)
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
        restore()
