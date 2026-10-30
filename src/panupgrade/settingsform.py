# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The form behind ``panupgrade settings``: every setting on one screen, grouped, with what it does and what it is set to.

Up and Down (or Tab) move between settings. Left and Right change a choice or a yes/no; numbers and text are typed. The
bottom of the screen explains the setting under the cursor and lists a caution for every setting that makes a run riskier.
Saving writes only the settings that differ from the built-in defaults, so a later, safer default still reaches you.

``Form`` holds the state and knows nothing about terminals; ``tui.show`` draws it.
"""

import json
import textwrap
from dataclasses import fields
from pathlib import Path

from . import settings as settings_mod
from .settings import Settings, SettingsError

ROWS, BUTTONS = "rows", "buttons"
BUTTON_LABELS = ["Save", "Save & Exit", "Quit without saving"]
SAVE_ONLY, SAVE_EXIT, QUIT = 0, 1, 2

SECTIONS = [
    ("HA behaviour", ["failover_band", "post_upgrade_band", "restore_roles", "sync_to_peer", "sync_timeout_s", "ha_config_unsynced",
                      "sync_content", "content_settle_s"]),
    ("Panorama range", ["spread_limit", "panorama_compare"]),
    ("Failure handling", ["max_failures", "max_parallel", "api_rate_per_s", "pending_changes"]),
    ("Downgrades", ["allow_downgrade", "allow_cross_feature_downgrade"]),
    ("Images, disk and time", ["image_source", "min_free_disk_mb", "download_timeout_s", "install_timeout_s", "reboot_timeout_s",
                               "poll_interval_s", "backup_dir", "approval_max_age_hours"]),
]
# Settings left out of the form on purpose (older credential-file paths): they are kept as they are and still written back.

# name -> (what it does, {choice or "true"/"false": what that value means})
INFO = {
    "failover_band": ("After the active member fails over, the new active member's session count must be within this fraction of what "
                      "it was before. 0.30 means plus or minus 30%.", {}),
    "post_upgrade_band": ("After the upgrade, the active member's session count must be within this fraction of the pair's baseline.", {}),
    "restore_roles": ("What to do with the HA roles once both members are upgraded.",
                      {"leave": "leave the roles as the upgrade left them",
                       "restore": "fail back, so the member that was active at the start is active again",
                       "pause": "stop and ask before touching the roles"}),
    "sync_to_peer": ("Whether the active member's configuration is pushed to the passive one, which forces the passive to match "
                     "what is running.",
                     {"never": "only report a pair that is out of sync; never force a sync",
                      "prepare": "the separate `prepare` step syncs ahead of the maintenance window",
                      "upgrade": "the upgrade run itself syncs each pair that needs it, overwriting the passive's configuration"}),
    "sync_timeout_s": ("How long to wait for a configuration sync to finish, in seconds.", {}),
    "ha_config_unsynced": ("A pair whose passive member's configuration is not synchronized. The risk is one moment: the active member "
                           "suspends and the upgraded passive takes over running its own, different configuration.",
                           {"block": "refuse to plan such a pair at all, so nobody runs into it unread",
                            "caution": "plan it anyway, flag that moment on the active member, and accept it when the batch is prepared"}),
    "sync_content": ("Dynamic content (applications and threats, antivirus, WildFire). HA members should run the same versions, "
                     "and a mismatch holds up the configuration sync.",
                     {"never": "leave content as it is",
                      "prepare": "install the latest on both members during `prepare`; the approval prompt lists it"}),
    "content_settle_s": ("After checking content on both members, how long to wait for PAN-OS's own HA check to read Match "
                         "before installing, in seconds.", {}),
    "spread_limit": ("Advise when a firewall is more than this many major versions behind Panorama. Advice only: it never blocks "
                     "an upgrade.", {}),
    "panorama_compare": ("How 'Panorama is older than the target' is judged.",
                         {"feature": "compare feature releases only (10.2 against 11.0)", "full": "compare every number"}),
    "max_failures": ("Stop starting new firewalls after this many have failed.", {}),
    "max_parallel": ("How many HA pairs (or standalone firewalls) are worked on at the same time. A pair's two members always go "
                     "one after the other.", {}),
    "api_rate_per_s": ("A polite ceiling on requests per second to Panorama, shared by every worker.", {}),
    "pending_changes": ("A firewall with uncommitted candidate configuration.",
                        {"block": "do not upgrade it", "warn": "upgrade it, with a warning", "ignore": "upgrade it without comment"}),
    "allow_downgrade": ("Install a release older than the one running. Same feature release only, and each one needs an "
                        "acknowledgement.", {"true": "downgrades are allowed", "false": "a release older than the running one is refused"}),
    "allow_cross_feature_downgrade": ("Also downgrade across a feature release (11.0 to 10.2). Needs allow_downgrade. A last resort: "
                                      "the newer release's configuration may not load on the older one.",
                                      {"true": "allowed", "false": "refused"}),
    "image_source": ("Who downloads the software image.",
                     {"firewall": "each firewall downloads it from the vendor", "panorama": "Panorama downloads it and pushes it"}),
    "min_free_disk_mb": ("Free disk space a firewall needs before an image is downloaded, in MB.", {}),
    "download_timeout_s": ("How long to wait for an image download, in seconds.", {}),
    "install_timeout_s": ("How long to wait for an install, in seconds.", {}),
    "reboot_timeout_s": ("How long to wait for a firewall to come back after a reboot, in seconds.", {}),
    "poll_interval_s": ("How often a running job or a rebooting firewall is checked, in seconds.", {}),
    "backup_dir": ("Folder where verified configuration backups are written before anything changes.", {}),
    "approval_max_age_hours": ("An approval older than this is refused. 168 is a week; 0 means it never expires.", {}),
}
LIMITS = {
    "failover_band": "a number above 0 and up to 1", "post_upgrade_band": "a number above 0 and up to 1",
    "api_rate_per_s": "a number above 0", "spread_limit": "a whole number, 1 or more", "max_failures": "a whole number, 1 or more",
    "max_parallel": "a whole number, 1 or more", "sync_timeout_s": "whole seconds, 1 or more",
    "poll_interval_s": "whole seconds, 1 or more", "content_settle_s": "whole seconds, 0 or more",
}
HELP = {
    ROWS: "Up / Down / Tab: move   Left / Right: change a choice   type: edit a number   Ctrl-R or Delete: back to the default",
    BUTTONS: "Left / Right: choose a button   Enter or Space: press it   Up: back to the settings",
}
ALWAYS = "Shortcuts: Ctrl-D = Save & Exit   Esc = Quit without saving (asks first if there are unsaved changes)"
NAME_WIDTH, VALUE_WIDTH, DEFAULT_WIDTH = 30, 14, 12


def _fmt(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, float) else str(value)


def _parse(kind, text):
    text = text.strip() if kind is not str else text
    if kind is bool:
        if text.lower() not in ("true", "false"):
            raise ValueError("must be true or false")
        return text.lower() == "true"
    try:
        return kind(text) if kind in (int, float) else text
    except ValueError:
        raise ValueError("must be a whole number" if kind is int else "must be a number") from None


def _kinds():
    return {f.name: f.type for f in fields(Settings)}


def shown_names():
    return [name for _title, names in SECTIONS for name in names]


def _short(name):
    """The first sentence of a setting's description, for the comment on its line in the file."""
    return INFO[name][0].split(". ")[0].rstrip(".") if name in INFO else ""


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value) if isinstance(value, str) else repr(value)


def write_file(path, settings):
    """Write only the settings that differ from the defaults, grouped and commented."""
    defaults, shown = Settings(), shown_names()
    other = [f.name for f in fields(Settings) if f.name not in shown]
    lines = ["# panupgrade settings. Only settings that differ from the built-in defaults are listed here; anything missing keeps",
             "# its default. Written by `panupgrade settings`: run it again to change them, or edit this file by hand.", ""]
    for title, names in SECTIONS + [("Other", other)]:
        picked = [n for n in names if getattr(settings, n) != getattr(defaults, n)]
        if picked:
            lines.append(f"# ---- {title} ----")
            lines += [f"{n} = {_toml(getattr(settings, n))}" + (f"   # {_short(n)}" if _short(n) else "") for n in picked]
            lines.append("")
    Path(path).write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


class Form:
    def __init__(self, current, path, saver=None, notice="", baseline=None):
        self.path = str(path)
        self.names = shown_names()
        self.kinds = _kinds()
        self.defaults = Settings()
        self.text = {n: _fmt(getattr(current, n)) for n in self.names}
        base = baseline or current
        self.saved = {n: _fmt(getattr(base, n)) for n in self.names}          # what is on disk, as far as the form knows
        self.kept = {f.name: getattr(current, f.name) for f in fields(Settings) if f.name not in self.names}
        self.saver = saver                                                    # saver(form, settings) writes the file, returns a message
        self.zone, self.focus, self.button = ROWS, 0, SAVE_EXIT
        self.offset, self.page = 0, 10
        self.message, self.armed = notice, False

    # ---- state
    def check(self):
        """(Settings or None, {setting: problem}, problem not tied to one setting)."""
        data, problems = dict(self.kept), {}
        for name in self.names:
            try:
                data[name] = _parse(self.kinds[name], self.text[name])
            except ValueError as exc:
                problems[name] = str(exc)
        if problems:
            return None, problems, ""
        try:
            return settings_mod.from_dict(data), {}, ""
        except SettingsError as exc:
            owner = next((n for n in self.names if str(exc).startswith(n)), None) or next((n for n in self.names if n in str(exc)), None)
            return None, ({owner: str(exc)} if owner else {}), ("" if owner else str(exc))

    def settings(self):
        return self.check()[0]

    def dirty(self):
        return self.text != self.saved

    def changed(self, name):
        return self.text[name] != _fmt(getattr(self.defaults, name))

    def changed_count(self):
        return sum(self.changed(n) for n in self.names)

    # ---- keys
    def press(self, key):
        """Apply one key. Returns 'accept' or 'cancel' when the form is finished, otherwise None."""
        was_armed, self.armed, self.message = self.armed, False, ""
        if key == "ctrl-c":
            return "cancel"
        if key == "esc":
            if self.dirty() and not was_armed:
                self.armed = True
                self.message = "!You have unsaved changes. Press Esc again to throw them away, or any other key to carry on."
                return None
            return "cancel"
        if key == "ctrl-d":
            return "accept" if self._save() else None
        if key in ("tab", "shift-tab"):
            self._step(1 if key == "tab" else -1)
        elif self.zone == BUTTONS:
            return self._button_key(key)
        else:
            self._row_key(key)
        return None

    def _step(self, delta):
        stops = len(self.names) + 1                                           # every setting, then the buttons
        here = len(self.names) if self.zone == BUTTONS else self.focus
        there = (here + delta) % stops
        self.zone, self.focus = (BUTTONS, len(self.names) - 1) if there == len(self.names) else (ROWS, there)
        if self.zone == BUTTONS:
            self.button = SAVE_EXIT

    def _row_key(self, key):
        name = self.names[self.focus]
        kind, text = self.kinds[name], self.text[name]
        choices = settings_mod._CHOICES.get(name)
        if key in ("down",):
            self._step(1)
        elif key == "up":
            self._step(-1)
        elif key == "pgdn":
            self.focus = min(self.focus + self.page, len(self.names) - 1)
        elif key == "pgup":
            self.focus = max(self.focus - self.page, 0)
        elif key == "home":
            self.focus = 0
        elif key == "end":
            self.focus = len(self.names) - 1
        elif key in ("ctrl-r", "delete"):
            self.text[name] = _fmt(getattr(self.defaults, name))
        elif choices and key in ("left", "right", " ", "enter"):
            at = choices.index(text) if text in choices else 0
            self.text[name] = choices[(at + (-1 if key == "left" else 1)) % len(choices)]
        elif kind is bool and key in ("left", "right", " ", "enter"):
            self.text[name] = "false" if text == "true" else "true"
        elif not choices and kind is not bool:
            if key == "backspace":
                self.text[name] = text[:-1]
            elif key == "ctrl-u":
                self.text[name] = ""
            elif len(key) == 1 and key.isprintable():
                self.text[name] = text + key

    def _button_key(self, key):
        if key == "left":
            self.button = max(self.button - 1, 0)
        elif key == "right":
            self.button = min(self.button + 1, len(BUTTON_LABELS) - 1)
        elif key == "up":
            self.zone = ROWS
        elif key in ("enter", " "):
            if self.button == SAVE_ONLY:
                self._save()
            elif self.button == SAVE_EXIT:
                return "accept" if self._save() else None
            else:
                return "cancel"
        return None

    def _save(self):
        current, problems, general = self.check()
        if current is None:
            first = next(iter(problems), None)
            self.message = "!" + (f"{first}: {problems[first]}" if first else general)
            if first:
                self.zone, self.focus = ROWS, self.names.index(first)
            return False
        if self.saver:
            try:
                self.message = self.saver(self, current)
            except OSError as exc:
                self.message = f"!Could not save: {exc.strerror or exc}"
                return False
        self.saved = dict(self.text)
        return True

    # ---- drawing
    def render(self, width, height, title=""):
        """The screen as a list of lines; each line is a list of (style, text) pieces. Fits width x height."""
        current, problems, general = self.check()
        bad = general or (f"{next(iter(problems))}: {next(iter(problems.values()))}" if problems else "")
        name = self.names[self.focus]
        lines = [[("bold", f" {title}".rstrip() or " panupgrade settings")], [("dim", " " + HELP[self.zone])], [("dim", " " + ALWAYS)]]
        status = self.message or ("!" + bad if bad else "")
        lines.append([("error" if status.startswith("!") else "dim", " " + status.lstrip("!"))])
        lines.append([])
        header = f"    {'setting'.ljust(NAME_WIDTH)}  {'value'.ljust(VALUE_WIDTH)}  {'default'.ljust(DEFAULT_WIDTH)}  what it does"
        lines.append([("bold", header)])

        body, focus_line = [], 0
        for title_, names in SECTIONS:
            body.append([("bold", f" {title_}")])
            for n in names:
                if n == name and self.zone == ROWS:
                    focus_line = len(body)
                body.append(self._row(n, n == name and self.zone == ROWS, n in problems, width))

        detail = self._detail(name, current, width)
        room = max(height - len(lines) - len(detail) - 3, 5)
        self.page = max(room - 2, 1)
        if focus_line < self.offset + 1:
            self.offset = max(focus_line - 1, 0)
        if focus_line >= self.offset + room:
            self.offset = focus_line - room + 1
        self.offset = max(0, min(self.offset, max(len(body) - room, 0)))
        lines += body[self.offset:self.offset + room]
        if len(body) > room:
            lines.append([("dim", f"   (showing lines {self.offset + 1}-{min(self.offset + room, len(body))} of {len(body)}; "
                                  f"{self.changed_count()} setting{'s' if self.changed_count() != 1 else ''} differ from the defaults)")])
        else:
            lines.append([("dim", f"   {self.changed_count()} setting{'s' if self.changed_count() != 1 else ''} differ from the defaults (marked *)")])
        lines += detail
        lines.append([])
        buttons = [("", "   ")]
        for n, label in enumerate(BUTTON_LABELS):
            on = self.zone == BUTTONS and self.button == n
            buttons += [("focus" if on else "bold", f"[ {label} ]"), ("", "  ")]
        if self.dirty():
            buttons.append(("warn", "unsaved changes"))
        lines.append(buttons)
        return [self._cut(line, width - 1) for line in lines[:height]]

    def _row(self, name, here, wrong, width):
        kind, text = self.kinds[name], self.text[name]
        cycles = name in settings_mod._CHOICES or kind is bool
        box = f"< {text} >" if cycles else f"[{text}{'_' if here else ''}]"
        if len(box) > VALUE_WIDTH:
            box = box[:VALUE_WIDTH - 2] + (">" if cycles else "]")
        marker = ("*" if self.changed(name) else " ")
        room = width - (NAME_WIDTH + VALUE_WIDTH + DEFAULT_WIDTH + 14)
        brief = textwrap.shorten(_short(name), room, placeholder="...") if room > 10 else ""
        style = "focus" if here else ("error" if wrong else "")
        return [(style, f" {'>' if here else ' '}{marker} {name.ljust(NAME_WIDTH)}  {box.ljust(VALUE_WIDTH)}  "),
                (style or "dim", _fmt(getattr(self.defaults, name)).ljust(DEFAULT_WIDTH)[:DEFAULT_WIDTH] + "  "),
                (style or "dim", brief)]

    def _detail(self, name, current, width):
        """What the setting under the cursor is, what its value means, and a caution for every risky setting in force."""
        text = self.text[name]
        kind = self.kinds[name]
        choices = settings_mod._CHOICES.get(name)
        what = f"one of: {', '.join(choices)}" if choices else "true or false" if kind is bool else LIMITS.get(name) or (
            "a whole number" if kind is int else "a number" if kind is float else "text")
        description, meanings = INFO[name]
        out = [[("bold", f" {name}"), ("dim", f"   ({what}; the default is {_fmt(getattr(self.defaults, name))})")]]
        for line in textwrap.wrap(description, max(width - 6, 20))[:2]:
            out.append([("", "   " + line)])
        if text in meanings:
            out.append([("bold", f"   {text}: "), ("", meanings[text])])
        cautions = current.loosening_warnings() if current is not None else []
        for warning in cautions[:3]:
            out.append([("warn", "   Caution: " + warning)])
        if len(cautions) > 3:
            out.append([("warn", f"   ... and {len(cautions) - 3} more cautions")])
        return out

    @staticmethod
    def _cut(line, width):
        out, used = [], 0
        for style, text in line:
            text = text[:max(width - used, 0)]
            used += len(text)
            if text:
                out.append((style, text))
        return out
