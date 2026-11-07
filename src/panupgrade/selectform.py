# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The form behind ``panupgrade select``: filters sit above the columns they filter, and two tables below them
(Available, Included) let you build the exact list of firewalls for an upgrade batch.

``Form`` holds the state and knows nothing about terminals, so it is tested by pressing keys at it. ``tui.show`` draws it
on a real terminal.
"""

from .planner import Selection
from .tui import plain, run, show, translate_windows_key  # noqa: F401 - re-exported: the form and its tests use them from here
from .versions import Version, VersionError

TEXT, LIST = "text", "list"
FILTER, AVAILABLE, INCLUDED, SAVE, BUTTONS = "filter", "available", "included", "save", "buttons"
BUTTON_LABELS = ["Save", "Save & Exit", "Deselect all", "Quit without saving"]
SAVE_ONLY, SAVE_EXIT, DESELECT_ALL, QUIT = 0, 1, 2, 3
INPUT, MODE = "input", "mode"
NOTHING = "everything Panorama manages"


class Column:
    """One table column and the filter(s) that sit above it. ``modes`` are (label, Selection attribute, kind, flag, example)."""
    def __init__(self, title, modes, cap, cell):
        self.title, self.modes, self.cap, self.cell = title, modes, cap, cell


COLUMNS = [
    Column("hostname", [("pattern", "name_glob", TEXT, "--name", "e.g. site1-*   (* matches anything, ? one character; the whole name must match)"),
                        ("contains", "name_contains", TEXT, "--name-contains", "any part of the name, e.g. gate"),
                        ("regex", "name_regex", TEXT, "--name-regex", "a regular expression, found anywhere in the name; ^ starts and $ ends it, . is any one character, e.g. ^site1-[ab]1$")],
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
    INCLUDED: "Space / Enter / D: take out   G: stand-alone group   C: clear the list   Up / Down / PgUp / PgDn: move   Tab: next section",
    SAVE: "Type the file name the list is saved to (empty: no file).   Enter: go to the buttons",
    BUTTONS: "Left / Right: choose a button   Enter or Space: press it   Up: back to the file name   (Deselect all empties the Included list)",
}
ALWAYS = "Shortcuts: Ctrl-D = Save & Exit   Esc = Quit without saving (asks first if there are unsaved changes)"


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
    def __init__(self, refs, initial=None, out="", notice="", saver=None):
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
        self.start = list(self.included)                       # what is on disk, as far as the form knows
        self._groups = {s: g for s, g in getattr(initial, "groups", {}).items() if s in self.included}   # serial -> stand-alone group name
        self.start_groups = dict(self._groups)
        self.editing = None                                    # the group name being typed for the firewall under the cursor, or None
        self.last_group = ""
        self.saver = saver                                     # saver(form) writes the list and returns a message
        self.button = SAVE_EXIT
        self.out = out or ""
        self.zone, self.fcol, self.frow = FILTER, 0, INPUT
        self.cursor = {AVAILABLE: 0, INCLUDED: 0}
        self.offset = {AVAILABLE: 0, INCLUDED: 0}
        self.page = 10
        self.message = notice
        self.armed = False

    @property
    def groups(self):
        """{serial: group name} for the firewalls that are Included (a firewall taken out loses its group)."""
        return {s: g for s, g in self._groups.items() if s in self.included and g}

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
        text = self.values["name_regex"].strip()
        if text and Selection.regex_problem(text):
            found["name_regex"] = Selection.regex_problem(text)
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
        if self.editing is not None:
            self._group_key(key)
            return None
        if key == "esc":
            if self.dirty() and not was_armed:
                self.armed = True
                self.message = "!Your Included list has not been saved. Press Esc again to throw it away, or any other key to carry on."
                return None
            return "cancel"
        if key == "ctrl-d":
            return self._save_and_exit()
        if key in ("tab", "shift-tab"):
            self._step(1 if key == "tab" else -1)
        elif self.zone == FILTER:
            self._filter_key(key)
        elif self.zone == SAVE:
            self._save_key(key)
        elif self.zone == BUTTONS:
            return self._button_key(key)
        else:
            self._table_key(key)
        return None

    def dirty(self):
        """True when the Included list differs from the last time it was saved (or from how it started)."""
        return self.included != self.start or self.groups != {s: g for s, g in self.start_groups.items() if s in self.start}

    def _group_key(self, key):
        """Typing the group name for the Included firewall under the cursor: Enter keeps it (empty removes the group), Esc leaves it as it was."""
        serial = self.included_refs()[self.cursor[INCLUDED]].serial
        if key == "enter":
            name = self.editing.strip().replace(",", " ").replace("#", " ")
            if name:
                self._groups[serial] = " ".join(name.split())
                self.last_group = self._groups[serial]
                self.message = f"Put {self._name(serial)} in the group {self.last_group}."
            else:
                self._groups.pop(serial, None)
                self.message = f"{self._name(serial)} is not in a group."
            self.editing = None
        elif key == "esc":
            self.editing = None
        elif key == "backspace":
            self.editing = self.editing[:-1]
        elif key == "ctrl-u":
            self.editing = ""
        elif len(key) == 1 and key.isprintable():
            self.editing += key

    def _name(self, serial):
        return next((r.hostname for r in self.refs if r.serial == serial), serial)

    def _problem(self):
        if self.errors():
            return "Fix the red filter first."
        if not self.included:
            return "The Included list is empty. Move firewalls into it from Available (Space), or press A to add all of them."
        return ""

    def _save(self, need_name):
        """Write the list now, if there is somewhere to write it. Returns True when the form may carry on to exit."""
        problem = self._problem()
        if not problem and need_name and not self.out.strip():
            problem = "Type a file name in 'Save the list to' first."
        if problem:
            self.message = "!" + problem
            return False
        if self.out.strip() and self.saver:
            try:
                self.message = self.saver(self)
            except OSError as exc:
                self.message = f"!Could not save: {exc.strerror or exc}"
                return False
        self.start, self.start_groups = list(self.included), dict(self.groups)
        return True

    def _save_and_exit(self):
        return "accept" if self._save(need_name=False) else None

    def _button_key(self, key):
        if key == "left":
            self.button = max(self.button - 1, 0)
        elif key == "right":
            self.button = min(self.button + 1, len(BUTTON_LABELS) - 1)
        elif key == "up":
            self.zone = SAVE
        elif key in ("enter", " "):
            if self.button == SAVE_ONLY:
                self._save(need_name=True)
            elif self.button == SAVE_EXIT:
                return self._save_and_exit()
            elif self.button == DESELECT_ALL:
                self.message = (f"Took all {len(self.included)} firewall{'s' if len(self.included) != 1 else ''} out of the Included list."
                                if self.included else "The Included list is already empty.")
                self.included = []
                self._clamp()
            else:
                return "cancel"
        return None

    def _step(self, delta):
        ring = [(FILTER, i) for i in range(len(COLUMNS))] + [(AVAILABLE, 0), (INCLUDED, 0), (SAVE, 0), (BUTTONS, 0)]
        here = self.fcol if self.zone == FILTER else len(COLUMNS) + [AVAILABLE, INCLUDED, SAVE, BUTTONS].index(self.zone)
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
        elif zone == INCLUDED and rows and key in ("g", "G"):
            self.editing = self._groups.get(rows[self.cursor[zone]].serial) or self.last_group
        elif zone == INCLUDED and rows and key in ("c", "C"):
            self.message = f"Cleared {len(rows)} firewall{'s' if len(rows) != 1 else ''} from the list."
            self.included = []
        self._clamp()

    def _save_key(self, key):
        if key in ("enter", "down"):
            self.zone, self.button = BUTTONS, SAVE_EXIT
        elif key == "up":
            self.zone = INCLUDED
        elif key == "backspace":
            self.out = self.out[:-1]
        elif key == "ctrl-u":
            self.out = ""
        elif len(key) == 1 and key.isprintable():
            self.out += key

    # ---- drawing
    def _widths(self, total):
        wide = []
        for column in COLUMNS:
            data = max([len(column.cell(r)) for r in self.refs] or [0])
            labels = max(len(label) + 4 for label, *_rest in column.modes)
            wide.append(max(MIN_WIDTH + 2, min(column.cap, max(data, len(column.title), labels))))
        named = [g for g in self.groups.values()]
        tag = min(max(len(g) for g in named) + 12, 30) if named else 0       # '  [group: name]' is drawn after the columns of an Included row
        room = total - 5 - 2 * (len(wide) - 1) - tag
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
        if self.editing is not None:
            status = (f"Group for {self._name(incl[self.cursor[INCLUDED]].serial)}: [{self.editing}_]   Enter keeps it, Esc leaves it, empty removes the group. "
                      f"Firewalls with the same name are upgraded together.")
        if not status and self.zone == FILTER and self.frow == INPUT:
            label, attr, kind, _flag, example = COLUMNS[self.fcol].modes[self.mode[self.fcol]]
            known = self.choices.get(attr) or []
            status = (f"This fleet has: {', '.join(known)}" if known else example)
        if not status and self.zone == FILTER and self.frow == MODE:
            status = "Left / Right changes the mode. Filters set in the other modes still apply: see 'Filters in effect' below."
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
        lines.append(self._in_effect())
        lines.append([])

        fixed = len(lines) + 7                                   # two table headings, the gaps before the second table and the save line
        room = max(height - fixed, 6)
        top = max(3, (room + 1) // 2)
        bottom = max(3, room - top)
        self.page = max(top - 1, 1)

        def table(zone, rows, shown, heading, note, empty):
            self._clamp()
            if zone == INCLUDED:
                lines.append([])
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
                line = cells(pieces, (">" if on else " ") + (" " if r.connected else "!"), "focus" if on else "")
                if zone == INCLUDED and self._groups.get(r.serial):
                    line.append(("focus" if on else "changed", f"[group: {self._groups[r.serial]}]"))
                lines.append(line)

        matches = self.matching()
        table(AVAILABLE, avail, top, "AVAILABLE", f"{len(matches)} of {len(self.refs)} firewalls match the filters" +
              (f", {len(matches) - len(avail)} of them already included" if len(matches) != len(avail) else "") + "   (! = offline)",
              "No firewall matches the filters, or all of them are already included.")
        table(INCLUDED, incl, bottom, "INCLUDED", f"{len(incl)} firewall{'s' if len(incl) != 1 else ''} for the batch",
              "Empty. Move firewalls here from Available with Space.")
        here = self.zone == SAVE
        lines.append([])
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Save the list to "),
                      ("focus" if here else "", f"[{self.out}{'_' if here else ''}".ljust(32) + "]"),
                      ("dim", "  plan, prepare and upgrade use this file when you don't name firewalls" if self.out.strip()
                       else "  not saved: later commands will need --serial or --serials-file")])
        lines.append([])
        buttons = [("", "   ")]
        for n, label in enumerate(BUTTON_LABELS):
            on = self.zone == BUTTONS and self.button == n
            buttons += [("focus" if on else "bold", f"[ {label} ]"), ("", "  ")]
        if self.dirty():
            buttons.append(("warn", "unsaved changes"))
        lines.append([(style, text) for style, text in buttons])
        return [self._cut(line, width - 1) for line in lines[:height]]

    def in_effect(self):
        """[(text, shown)] for every filter that has something typed in it, whether or not its mode is the one on screen."""
        found = []
        for i, column in enumerate(COLUMNS):
            for m, (label, attr, _kind, _flag, _eg) in enumerate(column.modes):
                text = self.values[attr].strip()
                if text:
                    found.append((f"{column.title} {label} {text}", m == self.mode[i]))
        return found

    def _in_effect(self):
        """The one line that says what is narrowing Available, so a filter hidden behind another mode is never a mystery."""
        found = self.in_effect()
        if not found:
            return [("dim", " Filters in effect: none, every firewall is listed")]
        line = [("bold", " Filters in effect: ")]
        for n, (text, shown) in enumerate(found):
            line += [("dim", "" if n == 0 else "  and  "), ("bold" if shown else "warn", text if shown else text + " (hidden, other mode)")]
        return line

    @staticmethod
    def _cut(line, width):
        out, used = [], 0
        for style, text in line:
            text = text[:max(width - used, 0)]
            used += len(text)
            if text:
                out.append((style, text))
        return out


