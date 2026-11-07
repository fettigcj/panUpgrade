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
    ("HA behaviour", []),
    ("  Active/Passive pairs", ["failover_band", "post_upgrade_band", "restore_roles", "upgrade_suspended", "pair_tolerate_asymmetry", "sync_to_peer", "sync_timeout_s",
                               "busy_wait_s", "settle_after_failover_s", "settle_after_reboot_s", "ha_config_unsynced", "blocked_peer", "sync_content", "content_settle_s"]),
    ("  Stand-alone groups", ["group_reboot_tolerance", "group_tolerate_asymmetry", "lb_drain_profile", "lb_drain_probe_protocol", "lb_drain_wait_s"]),
    ("Panorama range", ["spread_limit", "panorama_compare"]),
    ("Failure handling", ["max_failures", "max_parallel", "api_rate_per_s", "pending_changes"]),
    ("Downgrades", ["allow_downgrade"]),
    ("Images, disk and time", ["image_source", "delete_unneeded_images", "min_free_disk_mb", "download_timeout_s", "install_timeout_s", "reboot_timeout_s",
                               "poll_interval_s", "backup_dir", "approval_max_age_hours"]),
]
# name -> (what it does, {choice or "true"/"false": what that value means})
INFO = {
    "failover_band": ("After the active member fails over, the new active member's session count must be within this fraction of what "
                      "it was before. 0.30 means plus or minus 30%.", {}),
    "post_upgrade_band": ("After the upgrade, the active member's session count must be within this fraction of the pair's baseline.", {}),
    "pair_tolerate_asymmetry": ("Allow Asymmetric Return During Upgrade Window, for HA pairs. When the active member is suspended or reboots, the half-open TCP "
                                "handshakes it held are not synchronized to its peer; the peer then sees the rest of those flows as asymmetric and drops them. "
                                "On: before the first member of a pair is touched, the tool records each member's current values, sets `set session "
                                "tcp-reject-non-syn no` plus the persistent `deviceconfig setting session tcp-reject-non-syn no` and `deviceconfig setting tcp "
                                "asymmetric-path bypass` (committed on the firewall, so the pair must have no other uncommitted changes), and puts all three back, "
                                "deleting a setting that was not set when the pair is finished, successful or not. Every change and restore is "
                                "journaled; a firewall that could not be restored is reported loudly. Optional for pairs, since HA2 keeps ordinary sessions.",
                                {"true": "open the window for each pair that is upgraded",
                                 "false": "the default. Leave the firewalls' TCP settings alone"}),
    "group_tolerate_asymmetry": ("Allow Asymmetric Return During Upgrade Window, for stand-alone groups. A group has no HA2 link, so every existing session of "
                                 "the member that reboots lands on a surviving member as asymmetric traffic. On: before the first member of a group is "
                                 "touched, ALL its members are set to accept it (same two settings, recorded first and restored when the whole group is "
                                 "finished). Nearly mandatory for groups.",
                                 {"true": "the default. Open the window for each group that is upgraded",
                                  "false": "leave the TCP settings alone; sessions that move to another member will be dropped"}),
    "group_reboot_tolerance": ("A stand-alone group is a set of firewalls upgraded together that are not HA pairs (cloud load-balanced firewalls, for example); "
                               "groups are named in the firewall list (Choose firewalls, G on an Included firewall). This is how many members of one group may "
                               "be rebooting at the same time. 1 means one at a time. A group of seven with 5 upgrades five at once, then the other two. "
                               "A member that fails while it is out of service holds its slot, and the rest of its group is not started.", {}),
    "lb_drain_profile": ("The interface management profile a load balancer's probe relies on (an Azure load balancer probes a dataplane interface with SSH or "
                         "HTTP, which this profile must permit). Empty: never drain. Set: before a group member reboots, the protocols in lb_drain_probe_protocol are "
                         "taken out of THIS profile (a commit on the firewall, so the firewall must have no other uncommitted changes), the balancer sees "
                         "the probe fail and drains the member gracefully, and the profile is put back after the member is upgraded, or after it failed. "
                         "The profile must already exist on the firewall, and every interface that uses it is affected.", {}),
    "lb_drain_probe_protocol": ("What the load balancer probes the firewall with. Those protocols are taken out of the drain profile, so the probe "
                                "fails and the balancer drains the member. Most balancers probe with SSH or HTTP; a balancer that depends only on "
                                "ping is rare. A + means the profile permits both and both are taken out.",
                                {"ssh": "the default. Take SSH out of the profile", "http": "take HTTP out", "https": "take HTTPS out",
                                 "ssh+http": "take SSH and HTTP out", "ssh+https": "take SSH and HTTPS out", "http+https": "take HTTP and HTTPS out",
                                 "ssh+http+https": "take all three out", "ping": "take ping (ICMP) out"}),
    "lb_drain_wait_s": ("After the profile is changed, how long to wait for the load balancer to notice the failed probe and drain the member "
                        "before it reboots, in seconds. Set it above (probe interval x failures to mark it out) plus the drain time.", {}),
    "restore_roles": ("What to do with the HA roles once both members are upgraded.",
                      {"leave": "leave the roles as the upgrade left them",
                       "restore": "fail back, so the member that was active at the start is active again"}),
    "upgrade_suspended": ("An HA member that is suspended carries no traffic. Whether to upgrade it, treating it like a passive member (first in its pair). "
                          "The tool does not suspend it again, and does not re-enable it as a choice of its own: after the reboot the member "
                          "is expected to rejoin as passive, and the run fails that firewall with a clear message if it is still suspended.",
                          {"true": "the default. Upgrade a suspended member", "false": "a suspended member is blocked until someone makes it functional"}),
    "sync_to_peer": ("Whether the tool forces the passive member of an HA pair to match the active member's configuration. The sync "
                     "overwrites whatever the passive is running, so the passive's own configuration is backed up and verified first.",
                     {"Disable": "never force a sync. A pair whose passive is out of sync is only reported (and, depending on "
                                 "ha_config_unsynced, refused or flagged).",
                      "Both": "the default. Force the sync when preparing for the upgrade cycle (`prepare`) AND again during the upgrade "
                              "itself, just before the passive member is touched, for each pair that still needs it.",
                      "UpgradeOnly": "force the sync only during the upgrade itself. `prepare` leaves the pair alone."}),
    "sync_timeout_s": ("How long to wait for a configuration sync to finish, in seconds.", {}),
    "settle_after_failover_s": ("After the active member is suspended, wait this many seconds for the commit the firewall queues for itself, then until "
                                "its job list is empty, before the install. In the lab it started 18-24 s after the suspend and ran 65-113 s. "
                                "0 skips the window (the list must still be empty).", {}),
    "settle_after_reboot_s": ("After a reboot, wait this many seconds for the second auto-commit that follows the firewall reconnecting to Panorama "
                              "(4-52 s after it reconnects, 63-143 s long in the lab), then until its job list is empty. 0 skips the window.", {}),
    "busy_wait_s": ("How long to keep waiting when a firewall is busy, in seconds. Before an install and before checking a firewall that has "
                    "just rebooted, the tool waits for `show jobs all` to show nothing unfinished; if the firewall still refuses with "
                    "\"a commit is in progress\" it asks again every poll interval. Past this time the step fails.", {}),
    "ha_config_unsynced": ("Whether un-synchronized config should block upgrades or raise warnings.\n\n"
                           "If the passive node is upgraded while out-of-sync the active node may no longer be able to synchronize its "
                           "config to the higher-versioned peer. When the active node is upgraded and that peer becomes active whatever "
                           "config differences exist will go live. REVIEW CAREFULLY as interface address changes, Security or NAT policy "
                           "differences could cause catastrophic network changes.\n\n"
                           "Only used when sync_to_peer is Disable; with Both or UpgradeOnly the tool syncs the pair itself.",
                           {"block": "refuse to plan such a pair at all, so nobody runs into it unread",
                            "warn": "plan it anyway, flag that moment on the active member, and accept it when the batch is prepared"}),
    "blocked_peer": ("What to do with an HA member that could be upgraded when the other member of its pair is blocked by a check (or "
                     "could not be read). The active member's upgrade hands the traffic to its peer for the whole reboot, and either "
                     "upgrade leaves the pair on two releases. A peer that is only not selected, or already on the target, is not affected.",
                     {"block": "the default. Hold this member too, saying why, so half a pair is never upgraded against a problem",
                      "warn": "plan it anyway and flag it, to be accepted when the batch is prepared"}),
    "sync_content": ("Dynamic content (applications and threats, antivirus, WildFire). HA members should run the same versions, "
                     "and a mismatch holds up the configuration sync.",
                     {"never": "leave content as it is",
                      "prepare": "install the latest on both members during `prepare`; the approval prompt lists it"}),
    "content_settle_s": ("After checking content on both members, how long to wait for PAN-OS's own HA check to read Match "
                         "before installing, in seconds.", {}),
    "spread_limit": ("Advise (never block) when a firewall is more than this many versions behind Panorama, counted in the unit "
                     "panorama_compare names. The default of 2 with minor means Panorama should be at most 2 feature releases ahead of a "
                     "firewall: 12.2 and 12.1 and 11.2 are within 2 of each other.", {}),
    "panorama_compare": ("The unit PAN-OS releases are compared in, both for 'Panorama is older than the target' and for spread_limit. "
                         "A version reads major.minor.maintenance.hotfix: 12.1.10-h2 is major 12, minor 1, maintenance 10, hotfix 2.",
                         {"minor": "the default. Compare feature releases (major.minor). Distance is counted along the list of releases "
                                   "PAN-OS really has (12.2, 12.1, 11.2 and so on), because the numbering has gaps: there is no 12.0, so 11.2 is "
                                   "two behind 12.2. `panupgrade releases` shows that list and `--update` refreshes it.",
                          "major": "compare only the major number: 12 against 10 is two behind."}),
    "max_failures": ("Stop starting new firewalls after this many have failed.", {}),
    "max_parallel": ("How many units are worked on at the same time (default 5). Each HA pair is its own job: its two members go one after "
                     "the other, passive first, and it never waits for another pair, wherever that one is. A standalone firewall is a unit of one. "
                     "As soon as a unit finishes, the next one starts.", {}),
    "api_rate_per_s": ("A polite ceiling on requests per second to Panorama, shared by every worker.", {}),
    "pending_changes": ("A firewall with uncommitted candidate configuration.",
                        {"block": "do not upgrade it", "warn": "upgrade it, with a warning", "ignore": "upgrade it without comment"}),
    "allow_downgrade": ("Install a release older than the one running, to reach the target. Each firewall needs an acknowledgement. "
                        "Going down across a feature release follows the same skip rule as going up (10.1 or later, at most three "
                        "releases skipped), and the newer release's configuration may not load on the older one, so have a saved "
                        "configuration of the older release ready.",
                        {"true": "downgrades are allowed", "false": "the default. A release older than the running one is refused"}),
    "image_source": ("Who downloads the software image.",
                     {"firewall": "each firewall downloads it from the vendor", "panorama": "Panorama downloads it and pushes it"}),
    "delete_unneeded_images": ("During `prepare`, delete the PAN-OS images a firewall holds that this upgrade does not need, to make room. "
                               "It keeps the image that is running, the base image of the running release and of the target release "
                               "(normally x.y.0), every image on the upgrade path, and the target. The approval lists each deletion. "
                               "Nothing is deleted unless the target is known and the firewall's own image list could be read.",
                               {"true": "delete the other images during prepare",
                                "false": "the default. Never delete an image"}),
    "min_free_disk_mb": ("Free disk space a firewall needs before an image is downloaded, in MB.", {}),
    "download_timeout_s": ("How long to wait for an image download, in seconds.", {}),
    "install_timeout_s": ("How long to wait for an install, in seconds.", {}),
    "reboot_timeout_s": ("How long to wait for a firewall to come back after a reboot, in seconds (default 1920, 32 minutes). A reboot usually takes about 11 minutes; "
                         "the slowest seen, on an under-powered lab machine, took just over 30.", {}),
    "poll_interval_s": ("How often a running job or a rebooting firewall is checked, in seconds.", {}),
    "backup_dir": ("Folder where verified configuration backups are written before anything changes.", {}),
    "approval_max_age_hours": ("An approval older than this is refused. 168 is a week; 0 means it never expires.", {}),
}
LIMITS = {
    "failover_band": "a number above 0 and up to 1", "post_upgrade_band": "a number above 0 and up to 1",
    "api_rate_per_s": "a number above 0", "spread_limit": "a whole number, 1 or more", "max_failures": "a whole number, 1 or more",
    "max_parallel": "a whole number, 1 or more", "sync_timeout_s": "whole seconds, 1 or more", "busy_wait_s": "whole seconds, 1 or more",
    "poll_interval_s": "whole seconds, 1 or more", "lb_drain_wait_s": "whole seconds, 0 or more", "group_reboot_tolerance": "a whole number, 1 or more", "content_settle_s": "whole seconds, 0 or more",
    "settle_after_failover_s": "whole seconds, 0 or more", "settle_after_reboot_s": "whole seconds, 0 or more",
}
HELP = {
    ROWS: "Up / Down / Tab: move   Left / Right: change a choice   type: edit a number   Ctrl-R or Delete: back to the default   "
          "PgUp / PgDn: scroll the explanation",
    BUTTONS: "Left / Right: choose a button   Enter or Space: press it   Up: back to the settings",
}
ALWAYS = "Shortcuts: Ctrl-D = Save & Exit   Esc = Quit without saving (asks first if there are unsaved changes)"
NAME_WIDTH, VALUE_WIDTH = 30, 14
LEFT_WIDTH = 4 + NAME_WIDTH + 2 + VALUE_WIDTH + 2 + 10      # the list on the left; the explanation goes to its right when there is room
VERB_STYLE = {"block": "error", "warn": "warn"}               # the colour of a choice that blocks (red) or only warns (gold), wherever it is shown
SIDE_MIN = 44                                               # the least width the explanation needs beside the list


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
    return INFO[name][0].split("\n")[0].split(". ")[0].rstrip(".") if name in INFO else ""


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value) if isinstance(value, str) else repr(value)


def write_file(path, settings):
    """Write only the settings that differ from the defaults, grouped and commented."""
    defaults = Settings()
    lines = ["# panupgrade settings. Only settings that differ from the built-in defaults are listed here; anything missing keeps",
             "# its default. Written by `panupgrade settings`: run it again to change them, or edit this file by hand.", ""]
    for title, names in SECTIONS:
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
        self.saver = saver                                                    # saver(form, settings) writes the file, returns a message
        self.zone, self.focus, self.button = ROWS, 0, SAVE_EXIT
        self.offset, self.pane_offset, self.pane_page = 0, 0, 5
        self.message, self.armed = notice, False

    # ---- state
    def check(self):
        """(Settings or None, {setting: problem}, problem not tied to one setting)."""
        data, problems = {}, {}
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
        before = self._pane_key()
        result = self._press(key)
        if self._pane_key() != before and key not in ("pgup", "pgdn"):
            self.pane_offset = 0                                            # another setting or value: read it from the top
        return result

    def _pane_key(self):
        return self.focus, self.text[self.names[self.focus]]

    def _press(self, key):
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
            self.pane_offset += self.pane_page                       # the explanation scrolls; render() keeps it in range
        elif key == "pgup":
            self.pane_offset = max(self.pane_offset - self.pane_page, 0)
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
        elif key == "down":
            self.zone, self.focus = ROWS, 0                                   # round the ring to the first setting
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
        """The screen as a list of lines; each line is a list of (style, text) pieces. Fits width x height. Long text wraps."""
        current, problems, general = self.check()
        bad = general or (f"{next(iter(problems))}: {next(iter(problems.values()))}" if problems else "")
        name = self.names[self.focus]
        lines = [[("bold", f" {title}".rstrip() or " panupgrade settings")]]
        lines += self._wrapped("dim", HELP[self.zone], width, " ")
        if height >= 20:                                                    # a very short window keeps its lines for the settings
            lines += self._wrapped("dim", ALWAYS, width, " ")
        status = self.message or ("!" + bad if bad else "")
        lines += self._wrapped("error" if status.startswith("!") else "dim", status.lstrip("!"), width, " ")
        if height >= 20:
            lines.append([])
        lines.append([("bold", f"    {'setting'.ljust(NAME_WIDTH)}  {'value'.ljust(VALUE_WIDTH)}  default")])

        body, focus_line = [], 0
        for title_, names in SECTIONS:
            body.append([("bold", f" {title_}")])
            for n in names:
                if n == name and self.zone == ROWS:
                    focus_line = len(body)
                body.append(self._row(n, n == name and self.zone == ROWS, n in problems))

        side = self.side_by_side(width)
        avail = max(height - len(lines) - 3, 2)                             # rows for the list and, when stacked, the explanation
        if side:
            room = avail                                                    # rows beside the explanation; the header row is one more
            detail = self._scrolled(self._detail(name, current, width - LEFT_WIDTH - 4), room + 1)
        else:
            keep = 6 if avail >= 14 else max(avail // 3, 1)                 # rows the list keeps at the least
            detail = self._scrolled(self._detail(name, current, width), max(avail - keep, 2))
            room = max(avail - len(detail), 1)
        self.pane_page = max(len(detail) - 2, 1)
        if focus_line < self.offset + 1:
            self.offset = max(focus_line - 1, 0)
        if focus_line >= self.offset + room:
            self.offset = focus_line - room + 1
        self.offset = max(0, min(self.offset, max(len(body) - room, 0)))
        shown = body[self.offset:self.offset + room]
        if side:
            header = lines.pop()                                            # the explanation starts level with the column headings
            table = [header] + shown
            for i, left in enumerate(table):
                right = detail[i] if i < len(detail) else []
                lines.append(self._pad(left, LEFT_WIDTH) + [("dim", " | ")] + right)
        else:
            lines += shown
        count = f"{self.changed_count()} setting{'s' if self.changed_count() != 1 else ''} differ from the defaults (marked *)"
        lines.append([("dim", f"   showing lines {self.offset + 1}-{min(self.offset + room, len(body))} of {len(body)}; {count}"
                              if len(body) > room else f"   {count}")])
        if not side:
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

    @staticmethod
    def side_by_side(width):
        """True when the window is wide enough to put the explanation to the right of the list."""
        return width - 1 - LEFT_WIDTH - 3 >= SIDE_MIN

    @classmethod
    def _pad(cls, line, n):
        line = cls._cut(line, n)
        used = sum(len(text) for _style, text in line)
        return line + ([("", " " * (n - used))] if used < n else [])

    def _row(self, name, here, wrong):
        kind, text = self.kinds[name], self.text[name]
        cycles = name in settings_mod._CHOICES or kind is bool
        box = f"< {text} >" if cycles else f"[{text}{'_' if here else ''}]"
        if len(box) > VALUE_WIDTH:
            box = box[:VALUE_WIDTH - 3] + ("..>" if cycles else "..]")        # the whole value is printed in full under the list
        changed = self.changed(name)
        marker = "*" if changed else " "
        style = "focus" if here else ("error" if wrong else "changed" if changed else "")
        verb = VERB_STYLE.get(text) if cycles else None                        # block is red and warn is gold wherever they are chosen
        box_style = ("focus-" + verb if here else verb) if verb else style
        return [(style, f" {'>' if here else ' '}{marker} {name.ljust(NAME_WIDTH)}  "), (box_style, box.ljust(VALUE_WIDTH)), (style, "  "),
                (style or "dim", _fmt(getattr(self.defaults, name)))]

    @staticmethod
    def _wrapped(style, text, width, indent="   ", first=None):
        """``text`` broken into lines that fit ``width``, each a one-piece line. A newline in ``text`` starts a new paragraph (a blank
        line between paragraphs is written as two newlines). The first line starts with ``first`` (default ``indent``)."""
        out = []
        for n, paragraph in enumerate(text.split("\n")):
            if not paragraph.strip():
                out.append([(style, "")])
                continue
            lead = (indent if first is None else first) if n == 0 else indent
            wrapped = textwrap.wrap(paragraph, max(width - 2, len(lead) + 20), initial_indent=lead, subsequent_indent=indent,
                                    break_on_hyphens=False)
            out += [[(style, line)] for line in wrapped]
        return out

    def _scrolled(self, full, size):
        """The part of ``full`` that fits in ``size`` lines, starting where the person has scrolled to. When it does not all fit the
        last line says so and names the keys, so nobody is left unable to read the rest."""
        if len(full) <= size:
            self.pane_offset = 0
            return full
        window = size - 1
        self.pane_offset = max(0, min(self.pane_offset, len(full) - window))
        end = self.pane_offset + window
        return full[self.pane_offset:end] + [[("bold", f"   (PgUp / PgDn scroll this: lines {self.pane_offset + 1}-{end} of {len(full)})")]]

    def _detail(self, name, current, width):
        """What the setting under the cursor is, every choice it has and what each means (the current one marked), and a caution for
        every risky setting in force. All of it wraps."""
        text = self.text[name]
        kind = self.kinds[name]
        choices = settings_mod._CHOICES.get(name)
        what = f"one of: {', '.join(choices)}" if choices else "true or false" if kind is bool else LIMITS.get(name) or (
            "a whole number" if kind is int else "a number" if kind is float else "text")
        description, meanings = INFO[name]
        out = [[("bold", f" {name}"), ("dim", f"   ({what}; the default is {_fmt(getattr(self.defaults, name))})")]]
        out += self._wrapped("", description, width)
        if meanings:
            out.append([])
            for choice in (choices or list(meanings)):
                if choice in meanings:
                    here = choice == text
                    lead = f"   {'>' if here else ' '} {choice}: "
                    lines = self._wrapped("bold" if here else "", meanings[choice], width, "       ", lead)
                    if choice in VERB_STYLE:                                       # the verb itself carries its colour
                        style = lines[0][0][0]
                        lines[0] = [(VERB_STYLE[choice], lead.rstrip()), (style, lines[0][0][1][len(lead.rstrip()):])]
                    out += lines
        if kind is str and not choices and len(text) > VALUE_WIDTH - 2:
            out += self._wrapped("", text, width, "     ", "   Value: ")
        warnings = current.loosening_warnings() if current is not None else []
        if warnings:
            out.append([])
        for warning in warnings:
            out += self._wrapped("warn", warning, width, "     ", "   Warn: ")
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
