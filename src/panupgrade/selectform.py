# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The form behind ``panupgrade select``: filters sit above the columns they filter, and two tables below them
(Available, Included) let you build the exact list of firewalls for an upgrade batch.

``Form`` holds the state and knows nothing about terminals, so it is tested by pressing keys at it. ``show`` draws it
on a real terminal (standard library only: ``msvcrt`` on Windows, ``termios`` elsewhere).
"""

import os
import shutil
import sys

from .planner import Selection
from .versions import Version, VersionError

TEXT, LIST = "text", "list"
FILTER, AVAILABLE, INCLUDED, SAVE = "filter", "available", "included", "save"
INPUT, MODE = "input", "mode"
NOTHING = "everything Panorama manages"


class Column:
    """One table column and the filter(s) that sit above it. ``modes`` are (label, Selection attribute, kind, flag, example)."""
    def __init__(self, title, modes, cap, cell):
        self.title, self.modes, self.cap, self.cell = title, modes, cap, cell


COLUMNS = [
    Column("hostname", [("pattern", "name_glob", TEXT, "--name", "e.g. site1-*   (* matches anything, ? one character; the whole name must match)"),
                        ("contains", "name_contains", TEXT, "--name-contains", "any part of the name, e.g. gate")],
           28, lambda r: r.hostname),
    Column("serial", [("contains", "serials", LIST, "--serial", "part of a serial number; separate several with commas")], 16, lambda r: r.serial),
    Column("model", [("contains", "models", LIST, "--model", "part of a model name, e.g. VM or 850; add * yourself to anchor it, e.g. PA-8*")], 10, lambda r: r.model),
    Column("PAN-OS", [("is", "version_is", TEXT, "--version-is", "e.g. 10.2.1 or 10.2.*"),
                      ("older than", "version_older_than", TEXT, "--version-older-than", "e.g. 10.2.11   (running something below this)"),
                      ("newer than", "version_newer_than", TEXT, "--version-newer-than", "e.g. 10.2.2   (running something above this)")],
           14, lambda r: r.sw_version or "-"),
    Column("HA", [("contains", "ha_states", LIST, "--ha-state", "e.g. active, passive   (\"active\" also finds active-primary)")], 9, lambda r: r.ha_state or "-"),
    Column("device group", [("contains", "device_groups", LIST, "--device-group", "part of a device group name")], 16, lambda r: r.device_group or "-"),
    Column("tags", [("contains", "tags", LIST, "--tag", "part of a tag name")], 14, lambda r: ", ".join(r.tags or []) or "-"),
]
VERSION_ATTRS = ("version_older_than", "version_newer_than")
MIN_WIDTH = 8
HELP = {
    FILTER: "Tab: next filter   Up / Down: mode row, input row, tables   Left / Right: change mode or pick a known value",
    AVAILABLE: "Space / Enter: add to Included   A: add all listed   Up / Down / PgUp / PgDn: move   Tab: next section",
    INCLUDED: "Space / Enter / D: take out   C: clear the list   Up / Down / PgUp / PgDn: move   Tab: next section",
    SAVE: "Type a file name to save the list, or leave it empty to just look.   Enter: done",
}
ALWAYS = "Ctrl-D: done   Esc: cancel"


def _wrap(term):
    """A word typed into a 'contains' filter becomes *word*; one that already has a wildcard is taken as written."""
    return term if any(c in term for c in "*?[") else f"*{term}*"


def _split(text):
    return [part for part in text.replace(",", " ").split() if part]


def _choices(refs):
    """The values that exist in this fleet, per Selection attribute, so the form can offer them instead of making people guess."""
    def distinct(values):
        return sorted({v for v in values if v})

    def versions(values):
        def key(text):
            try:
                return (0, Version.parse(text))
            except VersionError:
                return (1, text)          # an unreadable version sorts last, as plain text
        return sorted({v for v in values if v}, key=key)
    found = versions(r.sw_version for r in refs)
    return {"version_is": found, "version_older_than": found, "version_newer_than": found,
            "ha_states": distinct((r.ha_state or "").lower() for r in refs), "models": distinct(r.model for r in refs),
            "device_groups": distinct(r.device_group for r in refs), "tags": distinct(t for r in refs for t in (r.tags or []))}


class Form:
    def __init__(self, refs, initial=None, out=""):
        self.refs = sorted(refs, key=lambda r: r.hostname.lower())
        self.choices = _choices(refs)
        self.values = {}
        initial = initial or Selection()
        for column in COLUMNS:
            for _label, attr, kind, _flag, _eg in column.modes:
                value = getattr(initial, attr)
                self.values[attr] = "" if attr == "serials" else (", ".join(value) if kind == LIST else (value or ""))
        self.mode = [0] * len(COLUMNS)
        for i, column in enumerate(COLUMNS):                  # open on the mode that already has something in it
            for m, (_l, attr, *_rest) in enumerate(column.modes):
                if self.values[attr]:
                    self.mode[i] = m
                    break
        wanted = set(initial.serials)                         # a list of serials given up front is the Included list itself
        self.included = [r.serial for r in self.refs if r.serial in wanted]
        self.start = list(self.included)
        self.out = out or ""
        self.zone, self.fcol, self.frow = FILTER, 0, INPUT
        self.cursor = {AVAILABLE: 0, INCLUDED: 0}
        self.offset = {AVAILABLE: 0, INCLUDED: 0}
        self.page = 10
        self.message = ""
        self.armed = False

    # ---- what the filters mean
    def errors(self):
        found = {}
        for attr in VERSION_ATTRS:
            text = self.values[attr].strip()
            if text:
                try:
                    Version.parse(text)
                except VersionError as exc:
                    found[attr] = str(exc)
        return found

    def filter_selection(self):
        """Every non-empty filter counts. A version that does not parse is left out so the tables keep working."""
        sel = Selection()
        bad = self.errors()
        for column in COLUMNS:
            for _label, attr, kind, _flag, _eg in column.modes:
                text = self.values[attr].strip()
                if text and attr not in bad:
                    setattr(sel, attr, [_wrap(t) for t in _split(text)] if kind == LIST else text)
        return sel

    def filters_active(self):
        return self.filter_selection().describe() != NOTHING

    def matching(self):
        sel = self.filter_selection()
        return [r for r in self.refs if sel.matches(r)]

    def available(self):
        have = set(self.included)
        return [r for r in self.matching() if r.serial not in have]

    def included_refs(self):
        have = set(self.included)
        return [r for r in self.refs if r.serial in have]

    def describe(self):
        """What to print as the criteria once the form closes."""
        if self.filters_active() and {r.serial for r in self.matching()} == set(self.included):
            return self.filter_selection().describe()
        return f"{len(self.included)} firewall{'s' if len(self.included) != 1 else ''} picked by hand"

    def flags(self):
        """Command-line flags that give the same list: the filters when they alone make it, otherwise the serial numbers."""
        sel = self.filter_selection()
        if self.filters_active() and {r.serial for r in self.matching()} == set(self.included):
            out = []
            for column in COLUMNS:
                for _label, attr, kind, flag, _eg in column.modes:
                    value = getattr(sel, attr)
                    out += [(flag, v) for v in value] if kind == LIST else ([(flag, value)] if value else [])
            return out
        return [("--serial", r.serial) for r in self.included_refs()]

    # ---- keys
    def press(self, key):
        """Apply one key. Returns 'accept' or 'cancel' when the form is finished, otherwise None."""
        was_armed, self.armed, self.message = self.armed, False, ""
        if key == "ctrl-c":
            return "cancel"
        if key == "esc":
            if self.included != self.start and not was_armed:
                self.armed = True
                self.message = "!Your Included list has not been saved. Press Esc again to throw it away, or any other key to carry on."
                return None
            return "cancel"
        if key == "ctrl-d":
            return self._finish()
        if key in ("tab", "shift-tab"):
            self._step(1 if key == "tab" else -1)
        elif self.zone == FILTER:
            self._filter_key(key)
        elif self.zone == SAVE:
            return self._save_key(key)
        else:
            self._table_key(key)
        return None

    def _finish(self):
        if self.errors():
            self.message = "!Fix the red filter first."
        elif not self.included:
            self.message = "!The Included list is empty. Move firewalls into it from Available (Space), or press A to add all of them."
        else:
            return "accept"
        return None

    def _step(self, delta):
        ring = [(FILTER, i) for i in range(len(COLUMNS))] + [(AVAILABLE, 0), (INCLUDED, 0), (SAVE, 0)]
        here = self.fcol if self.zone == FILTER else len(COLUMNS) + [AVAILABLE, INCLUDED, SAVE].index(self.zone)
        zone, col = ring[(here + delta) % len(ring)]
        if zone == FILTER:
            if self.zone != FILTER:
                self.frow = INPUT
            self.fcol = col
        self.zone = zone

    def _filter_key(self, key):
        column = COLUMNS[self.fcol]
        attr = column.modes[self.mode[self.fcol]][1]
        kind = column.modes[self.mode[self.fcol]][2]
        text = self.values[attr]
        if key == "up":
            self.frow = MODE
        elif key == "down":
            if self.frow == MODE:
                self.frow = INPUT
            else:
                self.zone = AVAILABLE
        elif key == "enter":
            if self.frow == MODE:
                self.frow = INPUT
            else:
                self.zone = AVAILABLE
        elif key in ("left", "right"):
            step = 1 if key == "right" else -1
            if self.frow == MODE:
                self.mode[self.fcol] = (self.mode[self.fcol] + step) % len(column.modes)
            else:
                self.values[attr] = self._pick(attr, kind, text, step)
        elif self.frow == INPUT:
            if key == "backspace":
                self.values[attr] = text[:-1]
            elif key == "ctrl-u":
                self.values[attr] = ""
            elif len(key) == 1 and key.isprintable():
                self.values[attr] = text + key
        self._clamp()

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

    def _rows(self, zone):
        return self.available() if zone == AVAILABLE else self.included_refs()

    def _clamp(self):
        for zone in (AVAILABLE, INCLUDED):
            self.cursor[zone] = max(0, min(self.cursor[zone], len(self._rows(zone)) - 1))

    def _table_key(self, key):
        zone = self.zone
        rows = self._rows(zone)
        last = len(rows) - 1
        if key == "up":
            if self.cursor[zone] > 0:
                self.cursor[zone] -= 1
            elif zone == AVAILABLE:
                self.zone, self.frow = FILTER, INPUT
            else:
                self.zone = AVAILABLE
                self.cursor[AVAILABLE] = max(len(self.available()) - 1, 0)
        elif key == "down":
            if self.cursor[zone] < last:
                self.cursor[zone] += 1
            else:
                self.zone = INCLUDED if zone == AVAILABLE else SAVE
        elif key == "pgup":
            self.cursor[zone] = max(self.cursor[zone] - self.page, 0)
        elif key == "pgdn":
            self.cursor[zone] = max(min(self.cursor[zone] + self.page, last), 0)
        elif key == "home":
            self.cursor[zone] = 0
        elif key == "end":
            self.cursor[zone] = max(last, 0)
        elif rows and key in (" ", "enter") or (zone == INCLUDED and rows and key in ("d", "D", "delete")):
            if zone == AVAILABLE:
                self.included.append(rows[self.cursor[zone]].serial)
                self.message = f"Added {rows[self.cursor[zone]].hostname}."
            else:
                gone = rows[self.cursor[zone]]
                self.included.remove(gone.serial)
                self.message = f"Took {gone.hostname} out."
        elif zone == AVAILABLE and rows and key in ("a", "A"):
            self.included += [r.serial for r in rows]
            self.message = f"Added {len(rows)} firewall{'s' if len(rows) != 1 else ''}."
        elif zone == INCLUDED and rows and key in ("c", "C"):
            self.message = f"Cleared {len(rows)} firewall{'s' if len(rows) != 1 else ''} from the list."
            self.included = []
        self._clamp()

    def _save_key(self, key):
        if key == "enter":
            return self._finish()
        if key == "up":
            self.zone = INCLUDED
        elif key == "backspace":
            self.out = self.out[:-1]
        elif key == "ctrl-u":
            self.out = ""
        elif len(key) == 1 and key.isprintable():
            self.out += key
        return None

    # ---- drawing
    def _widths(self, total):
        wide = []
        for column in COLUMNS:
            data = max([len(column.cell(r)) for r in self.refs] or [0])
            labels = max(len(label) + 4 for label, *_rest in column.modes)
            wide.append(max(MIN_WIDTH + 2, min(column.cap, max(data, len(column.title), labels))))
        room = total - 5 - 2 * (len(wide) - 1)
        while sum(wide) > room and max(wide) > MIN_WIDTH + 2:      # trim the widest column a little at a time
            wide[wide.index(max(wide))] -= 1
        return wide

    def render(self, width, height, title=""):
        """The screen as a list of lines; each line is a list of (style, text) pieces. Fits width x height."""
        widths = self._widths(width)
        errors = self.errors()
        avail, incl = self.available(), self.included_refs()
        lines = [[("bold", f" {title}".rstrip() or " panupgrade select")],
                 [("dim", " " + HELP[self.zone])], [("dim", " " + ALWAYS)]]
        status = self.message
        if not status and self.zone == FILTER and self.frow == INPUT:
            label, attr, kind, _flag, example = COLUMNS[self.fcol].modes[self.mode[self.fcol]]
            known = self.choices.get(attr) or []
            status = (f"This fleet has: {', '.join(known)}" if known else example)
        lines.append([("error" if status.startswith("!") else "dim", " " + status.lstrip("!"))])
        lines.append([])

        def cells(pieces, marker="", row=""):
            line = [(row, " " + marker.ljust(3))]
            for i, (style, text) in enumerate(pieces):
                line += [(row or style, text[:widths[i]].ljust(widths[i])), (row, "  ")]
            return line

        lines.append([("bold", " FILTERS"), ("dim", "  (they narrow the Available table)")])
        lines.append(cells([("bold", c.title) for c in COLUMNS]))
        mode_row, input_row = [], []
        for i, column in enumerate(COLUMNS):
            here = self.zone == FILTER and self.fcol == i
            label, attr, kind, _flag, _eg = column.modes[self.mode[i]]
            arrows = f"< {label} >" if len(column.modes) > 1 else label
            mode_row.append(("focus" if here and self.frow == MODE else "dim", arrows))
            text = self.values[attr]
            room = widths[i] - 3
            shown = ("..." + text[-(room - 3):]) if len(text) > room else text
            bad = any(a in errors for _l, a, *_r in column.modes)
            style = ("focus-error" if here and self.frow == INPUT else "error") if bad else ("focus" if here and self.frow == INPUT else "")
            input_row.append((style, f"[{shown}{'_' if here and self.frow == INPUT else ''}".ljust(widths[i] - 1) + "]"))
        lines.append(cells(mode_row))
        lines.append(cells(input_row))
        for attr, text in ((a, errors[a]) for a in errors):
            lines.append([("error", f"   {attr.replace('_', ' ')}: {text}")])
        lines.append([])

        fixed = len(lines) + 3                                   # two table headings and the save line
        room = max(height - fixed, 6)
        top = max(3, (room + 1) // 2)
        bottom = max(3, room - top)
        self.page = max(top - 1, 1)

        def table(zone, rows, shown, heading, note, empty):
            self._clamp()
            here = self.zone == zone
            cursor, off = self.cursor[zone], self.offset[zone]
            if cursor < off:
                off = cursor
            if cursor >= off + shown:
                off = cursor - shown + 1
            off = self.offset[zone] = max(0, min(off, max(len(rows) - shown, 0)))
            span = f"   (showing {off + 1}-{min(off + shown, len(rows))} of {len(rows)})" if len(rows) > shown else ""
            lines.append([("focus" if here else "bold", f" {'>' if here else ' '} {heading}"), ("dim", f"  {note}{span}")])
            if not rows:
                lines.append([("dim", "     " + empty)])
            for n, r in enumerate(rows[off:off + shown], off):
                pieces = [("", column.cell(r)) for column in COLUMNS]
                on = n == cursor and here
                lines.append(cells(pieces, (">" if on else " ") + (" " if r.connected else "!"), "focus" if on else ""))

        matches = self.matching()
        table(AVAILABLE, avail, top, "AVAILABLE", f"{len(matches)} of {len(self.refs)} firewalls match the filters" +
              (f", {len(matches) - len(avail)} of them already included" if len(matches) != len(avail) else "") + "   (! = offline)",
              "No firewall matches the filters, or all of them are already included.")
        table(INCLUDED, incl, bottom, "INCLUDED", f"{len(incl)} firewall{'s' if len(incl) != 1 else ''} for the batch",
              "Empty. Move firewalls here from Available with Space.")
        here = self.zone == SAVE
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Save the list to "),
                      ("focus" if here else "", f"[{self.out}{'_' if here else ''}".ljust(32) + "]"),
                      ("dim", "  (empty: just look)")])
        return [self._cut(line, width - 1) for line in lines[:height]]

    @staticmethod
    def _cut(line, width):
        out, used = [], 0
        for style, text in line:
            text = text[:max(width - used, 0)]
            used += len(text)
            if text:
                out.append((style, text))
        return out


def plain(lines):
    """The screen as plain text, for tests and for people who want to see it without colour."""
    return "\n".join("".join(text for _style, text in line) for line in lines)


CODES = {"bold": "\x1b[1m", "dim": "\x1b[2m", "focus": "\x1b[7m", "error": "\x1b[31m", "focus-error": "\x1b[7;31m", "": ""}


def draw(form, out, width, height, title=""):
    parts = ["\x1b[H"]
    for line in form.render(width, height, title):
        parts.append("".join(f"{CODES[style]}{text}\x1b[0m" if style else text for style, text in line) + "\x1b[K\r\n")
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
_ANSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "Z": "shift-tab", "H": "home", "F": "end",
              "5~": "pgup", "6~": "pgdn", "1~": "home", "7~": "home", "4~": "end", "8~": "end", "3~": "delete"}
_PLAIN_KEYS = {"\r": "enter", "\n": "enter", "\t": "tab", "\x7f": "backspace", "\x08": "backspace", "\x15": "ctrl-u", "\x03": "ctrl-c",
               "\x04": "ctrl-d"}


_VK_KEYS = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup", 0x22: "pgdn", 0x24: "home", 0x23: "end",
            0x2E: "delete", 0x0D: "enter", 0x1B: "esc", 0x08: "backspace"}
_SHIFT = 0x10


def translate_windows_key(vk, char, state):
    """One Windows console key event as a key name. Reading the event itself (not msvcrt) is what reveals Shift on Tab."""
    if vk == 0x09:
        return "shift-tab" if state & _SHIFT else "tab"
    if vk in _VK_KEYS:
        return _VK_KEYS[vk]
    if char and char != chr(0):
        return _PLAIN_KEYS.get(char, char)
    return ""                                   # Shift, Ctrl, Alt and the like on their own


def _read_key_windows():
    import ctypes

    class KeyEvent(ctypes.Structure):
        _fields_ = [("down", ctypes.c_int32), ("repeat", ctypes.c_uint16), ("vk", ctypes.c_uint16), ("scan", ctypes.c_uint16),
                    ("char", ctypes.c_wchar), ("state", ctypes.c_uint32)]

    class Union(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("pad", ctypes.c_byte * 16)]

    class Record(ctypes.Structure):
        _fields_ = [("kind", ctypes.c_uint16), ("event", Union)]

    kernel = ctypes.windll.kernel32
    handle = kernel.GetStdHandle(-10)
    record, count = Record(), ctypes.c_uint32()
    while True:
        if not kernel.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(count)):
            raise OSError("cannot read keys from this console")
        key = record.event.key
        if record.kind == 1 and key.down:                # a key going down; ignore releases, mouse and resize events
            name = translate_windows_key(key.vk, key.char, key.state)
            if name:
                return name


def _read_key_posix(fd):
    import select
    first = os.read(fd, 1)
    if first == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        if os.read(fd, 1) not in (b"[", b"O"):
            return ""
        seq = ""
        while select.select([fd], [], [], 0.05)[0]:
            tail = os.read(fd, 1).decode("ascii", "replace")
            seq += tail
            if tail.isalpha() or tail == "~":
                break
        return _ANSI_KEYS.get(seq, "")
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
    except KeyboardInterrupt:
        return False
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
        restore()
