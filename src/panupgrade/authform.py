# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The form behind ``panupgrade auth``: Panorama profiles, and the admin accounts each one carries.

Each profile (see ``profiles``) is one Panorama: its address and its own credentials file of admin accounts, with a choice of which
account reads and which makes changes. Passwords and API keys are typed hidden, go into that profile's credentials file and nowhere
else, and are never shown again: the list shows only each account's name and what kind of secret it holds. Which profile is in use
is remembered automatically, or (Windows) kept in the user-level variable PANUPGRADE_PROFILE.

Everything here is staged: nothing reaches the disk until Save. ``Form`` holds the state and knows nothing about terminals or the
network; what needs the network (obtaining an API key, testing a sign-in, accepting a certificate) comes in through ``services``,
so this module can be tested by pressing keys at it.
"""

import copy
import os
import textwrap
from pathlib import Path

from . import credstore, profiles, trust, userenv
from .sources import SourceError
from .trust import CertificateProblem

PROFILE, ADDRESS, ACCOUNTS, ACTIONS, STORE, BUTTONS, MODAL, PROMPT = ("profile", "address", "accounts", "actions", "store", "buttons", "modal",
                                                                      "prompt")
RING = [PROFILE, ADDRESS, ACCOUNTS, ACTIONS, STORE, BUTTONS]
ACTION_LABELS = ["Add account", "Remove account", "Use for reading", "Use for changes", "Test sign-in", "New profile", "Remove profile",
                 "Forget certificate"]
ADD_ACCOUNT, REMOVE_ACCOUNT, FOR_READING, FOR_CHANGES, TEST, NEW_PROFILE, REMOVE_PROFILE, FORGET = range(8)
BUTTON_LABELS = ["Save", "Save & Exit", "Quit without saving"]
SAVE_ONLY, SAVE_EXIT, QUIT = 0, 1, 2
STORES = {"state": "remembered automatically", "env": "the user environment variable PANUPGRADE_PROFILE (this Windows account)"}
KINDS = ["password", "API key", "password, then keep only the API key"]
KIND_HELP = [
    "The password is stored in this profile's credentials file and used to sign in at the start of each run.",
    "A ready-made API key is stored in this profile's credentials file and used as it is; no password is kept anywhere.",
    "You type the password once; the tool signs in to Panorama with it now, keeps only the API key it gets back, and forgets the password. "
    "Needs the profile's address. The account name must be the PAN-OS user name.",
]
HELP = {
    PROFILE: "Left / Right: switch Panorama profile   N: new profile   Delete: remove this profile   Tab: next",
    ADDRESS: "Type this profile's Panorama address (also used to test a sign-in or obtain an API key).   Tab: next",
    ACCOUNTS: "Up / Down: choose an account   A: add   Delete: remove   R: use for reading   C: use for changes   T: test   Tab: next",
    ACTIONS: "Left / Right: choose an action   Enter: do it   Tab: next",
    STORE: "Left / Right: choose how the profile in use is remembered.   Tab: next",
    BUTTONS: "Left / Right: choose a button   Enter or Space: press it   Tab: next",
    MODAL: "Tab: next field   Left / Right: change the choice   Enter: next / press   Esc: cancel",
    PROMPT: "Left / Right: choose   Enter: press it   Esc: do not trust it",
}
ALWAYS = "Ctrl-D = Save & Exit   Esc = Quit without saving (asks first if there are unsaved changes)"
ACCOUNT_ENV = {"PANUPGRADE_CREDS": "the credentials file", "PANUPGRADE_ACCOUNT": "the account that reads",
               "PANUPGRADE_WORK_ACCOUNT": "the account that makes changes"}


def _wrapped(style, text, width, indent=" "):
    return [[(style, line)] for line in textwrap.wrap(text, max(width - 2, 20), initial_indent=indent, subsequent_indent=indent,
                                                       break_on_hyphens=False)] or [[]]


class Entry:
    """One profile as the form holds it: staged changes to its address, accounts and choices."""

    def __init__(self, name, address="", accounts=None, reading="", changes="", creds_path="", new=False):
        self.name, self.address, self.accounts = name, address, copy.deepcopy(accounts or {})
        self.reading, self.changes, self.creds_path = reading, changes, creds_path
        self.new, self.removed = new, False
        self.creds_exists = Path(creds_path).is_file() if creds_path else False
        self.saved = self.state()

    def state(self):
        return (self.address, copy.deepcopy(self.accounts), self.reading, self.changes, self.removed, self.new)

    def accounts_changed(self):
        return self.accounts != self.saved[1]


class Form:
    def __init__(self, entries, active="", store="state", notice="", services=None, overrides=(), draft_address=""):
        self.entries = {e.name: e for e in entries}
        names = self.names()
        self.active = active if active in names else (names[0] if names else "")
        self.store = store
        self.services, self.overrides = services, list(overrides)
        self.zone, self.cursor, self.action, self.button = PROFILE if not self.active else ACCOUNTS, 0, ADD_ACCOUNT, SAVE_EXIT
        self.modal = None                                     # the new-account or new-profile fields while they are open
        self.prompt = None                                    # "accept this certificate?" while it is being asked
        self.exit_after_trust = False                         # opened because a step hit a certificate problem: finish once it is settled
        self.message, self.armed, self.ever_saved = notice, False, False
        self.saved = (self.active, self.store)
        unmatched = draft_address and not any(e.address.lower() == draft_address.strip().lower() for e in self.entries.values())
        if not self.entries or unmatched:                     # nothing yet for this Panorama: the first thing to do is make a profile
            self.modal = {"type": "profile", "field": 0, "name": "", "address": draft_address}
            self.message = notice or "Make a profile for this Panorama first: a name for it, and its address."

    # ---- state
    def names(self):
        """The profiles to show, alphabetically (those staged for removal are hidden)."""
        return sorted(n for n, e in self.entries.items() if not e.removed)

    @property
    def entry(self):
        return self.entries.get(self.active)

    accounts = property(lambda self: self.entry.accounts if self.entry else {})
    address = property(lambda self: self.entry.address if self.entry else "",
                       lambda self, v: setattr(self.entry, "address", v) if self.entry else None)
    reading = property(lambda self: self.entry.reading if self.entry else "",
                       lambda self, v: setattr(self.entry, "reading", v) if self.entry else None)
    changes = property(lambda self: self.entry.changes if self.entry else "",
                       lambda self, v: setattr(self.entry, "changes", v) if self.entry else None)
    creds_path = property(lambda self: self.entry.creds_path if self.entry else "")
    creds_exists = property(lambda self: self.entry.creds_exists if self.entry else None)

    def account_names(self):
        return sorted(self.accounts)

    def current(self):
        names = self.account_names()
        return names[self.cursor] if names else None

    def effective_reading(self):
        return self.reading if self.reading in self.accounts else (self.account_names()[0] if len(self.accounts) == 1 else "")

    def effective_changes(self):
        return self.changes if self.changes in self.accounts else self.effective_reading()

    def dirty(self):
        return (self.active, self.store) != self.saved or any(e.state() != e.saved for e in self.entries.values())

    def mark_saved(self):
        self.entries = {n: e for n, e in self.entries.items() if not e.removed}
        for e in self.entries.values():
            e.new, e.creds_exists = False, Path(e.creds_path).is_file()
            e.saved = e.state()
        self.saved = (self.active, self.store)

    # ---- keys
    def press(self, key):
        """Apply one key. Returns 'accept' or 'cancel' when the form is finished, otherwise None."""
        was_armed, self.armed, self.message = self.armed, False, ""
        if key == "ctrl-c":
            return "cancel"
        if self.prompt is not None:
            return self._prompt_key(key)
        if self.modal is not None:
            self._modal_key(key)
            return None
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
        elif self.zone == PROFILE:
            self._profile_key(key)
        elif self.zone == ADDRESS:
            self._text_key(key, "address")
        elif self.zone == STORE:
            self._store_key(key)
        elif self.zone == ACCOUNTS:
            self._accounts_key(key)
        elif self.zone == ACTIONS:
            self._actions_key(key)
        else:
            return self._button_key(key)
        return None

    def _step(self, delta):
        self.zone = RING[(RING.index(self.zone) + delta) % len(RING)]

    def _profile_key(self, key):
        names = self.names()
        if key in ("left", "right") and names:
            self.active = names[(names.index(self.active) + (1 if key == "right" else -1)) % len(names)]
            self.cursor = 0
        elif key in ("n", "N"):
            self._do(NEW_PROFILE)
        elif key == "delete":
            self._do(REMOVE_PROFILE)
        elif key in ("down", "enter"):
            self._step(1)
        elif key == "up":
            self._step(-1)

    def _text_key(self, key, attr):
        if self.entry is None:
            return
        text = getattr(self, attr)
        if key in ("down", "enter"):
            self._step(1)
        elif key == "up":
            self._step(-1)
        elif key == "backspace":
            setattr(self, attr, text[:-1])
        elif key == "ctrl-u":
            setattr(self, attr, "")
        elif len(key) == 1 and key.isprintable():
            setattr(self, attr, text + key)

    def _store_key(self, key):
        if key in ("left", "right", " ", "enter") and userenv.supported():
            self.store = "env" if self.store == "state" else "state"
        elif key in ("left", "right", " ", "enter"):
            self.message = "!This system has no user-level environment variables to set, so the profile in use is remembered automatically."
        elif key == "down":
            self._step(1)
        elif key == "up":
            self._step(-1)

    def _accounts_key(self, key):
        names = self.account_names()
        if key == "up":
            if self.cursor > 0:
                self.cursor -= 1
            else:
                self._step(-1)
        elif key == "down":
            if self.cursor < len(names) - 1:
                self.cursor += 1
            else:
                self._step(1)
        elif key in ("a", "A"):
            self._do(ADD_ACCOUNT)
        elif key == "delete":
            self._do(REMOVE_ACCOUNT)
        elif key in ("r", "R"):
            self._do(FOR_READING)
        elif key in ("c", "C"):
            self._do(FOR_CHANGES)
        elif key in ("t", "T"):
            self._do(TEST)

    def _actions_key(self, key):
        if key == "left":
            self.action = max(self.action - 1, 0)
        elif key == "right":
            self.action = min(self.action + 1, len(ACTION_LABELS) - 1)
        elif key == "up":
            self._step(-1)
        elif key == "down":
            self._step(1)
        elif key in ("enter", " "):
            self._do(self.action)

    def _button_key(self, key):
        if key == "left":
            self.button = max(self.button - 1, 0)
        elif key == "right":
            self.button = min(self.button + 1, len(BUTTON_LABELS) - 1)
        elif key == "up":
            self._step(-1)
        elif key == "down":
            self._step(1)
        elif key in ("enter", " "):
            if self.button == SAVE_ONLY:
                self._save()
            elif self.button == SAVE_EXIT:
                return "accept" if self._save() else None
            else:
                return "cancel"
        return None

    # ---- actions
    def _do(self, action):
        name = self.current()
        if action == NEW_PROFILE:
            self.modal = {"type": "profile", "field": 0, "name": "", "address": ""}
        elif action == REMOVE_PROFILE:
            self._remove_profile()
        elif action == FORGET:
            self._forget()
        elif self.entry is None:
            self.message = "!Make a profile first (New profile)."
        elif action == ADD_ACCOUNT:
            self.modal = {"type": "account", "field": 0, "name": "", "kind": 0, "secret": ""}
        elif name is None:
            self.message = "!There are no accounts yet. Add one first."
        elif action == REMOVE_ACCOUNT:
            del self.accounts[name]
            self.reading = "" if self.reading == name else self.reading
            self.changes = "" if self.changes == name else self.changes
            self.cursor = max(min(self.cursor, len(self.accounts) - 1), 0)
            self.message = f"Removed {name} (it is taken out of the file when you save)."
        elif action == FOR_READING:
            self.reading = name
            self.message = f"{name} will be used for reading."
        elif action == FOR_CHANGES:
            self.changes = "" if self.changes == name else name
            self.message = (f"{name} will be used for changes." if self.changes else "Changes will use the same account as reading.")
        elif action == TEST:
            self._test(name)

    def _remove_profile(self):
        entry = self.entry
        if entry is None:
            return
        if entry.new:
            del self.entries[entry.name]
            self.message = f"Dropped the new profile {entry.name}."
        else:
            entry.removed = True
            self.message = f"{entry.name} will be removed when you save. Its folder is kept (renamed), so no backup or journal is lost."
        names = self.names()
        self.active, self.cursor = (names[0] if names else ""), 0
        if not names:
            self.modal = {"type": "profile", "field": 0, "name": "", "address": ""}

    def _test(self, name):
        if not self.address.strip():
            self.message, self.zone = "!Type this profile's Panorama address first (the Address field).", ADDRESS
        elif self.services is None:
            self.message = "!Testing is not available here."
        else:
            try:
                ok, text = self.services.test(self.address.strip(), name, self.accounts[name])
            except CertificateProblem as problem:
                self.ask_about_certificate(problem, lambda: self._test(name))
                return
            except SourceError as exc:
                ok, text = False, str(exc)
            self.message = ("" if ok else "!") + text

    def trusted(self):
        """The certificate accepted for the address typed above (or None), for showing."""
        if not self.address.strip() or self.services is None or not hasattr(self.services, "trusted"):
            return None
        return self.services.trusted(self.address.strip())

    def _forget(self):
        if not self.address.strip():
            self.message, self.zone = "!Type the Panorama address first (the Address field).", ADDRESS
        elif self.services is None or not hasattr(self.services, "forget"):
            self.message = "!Not available here."
        elif self.services.forget(self.address.strip()):
            self.message = f"Forgot the certificate accepted for {self.address.strip()}. The next connection will ask again."
        else:
            self.message = f"No certificate had been accepted for {self.address.strip()}."

    def ask_about_certificate(self, problem, retry):
        """Ask whether to trust the certificate Panorama presented. ``retry`` runs again, once it is trusted."""
        self.prompt = {"problem": problem, "retry": retry, "choice": 0 if problem.sha256 else 1}

    def _prompt_key(self, key):
        prompt = self.prompt
        can_accept = bool(prompt["problem"].sha256)
        if key in ("left", "right", "tab", "shift-tab") and can_accept:
            prompt["choice"] = 1 - prompt["choice"]
        elif key == "esc" or (key in ("enter", " ") and prompt["choice"] == 1):
            self.prompt = None
            self.message = "Not trusted, so nothing was sent to that Panorama."
        elif key in ("enter", " ") and can_accept:
            problem, retry = prompt["problem"], prompt["retry"]
            self.prompt = None
            try:
                self.services.trust(problem.address, problem.sha256, problem.reason)
            except OSError as exc:
                self.message = f"!Could not remember the certificate: {exc.strerror or exc}"
                return None
            self.message = f"Trusting exactly that certificate for {problem.address} from now on."
            if self.exit_after_trust:
                return "accept"
            retry()
        return None

    # ---- the two small dialogs
    def _modal_key(self, key):
        m = self.modal
        fields = 5 if m["type"] == "account" else 4            # account: name, kind, secret, [Add], [Cancel]; profile: name, address, [Create], [Cancel]
        buttons_at = fields - 2
        if key == "esc":
            self.modal = None
            if not self.entries:
                self.message = "!Nothing can be set up until there is a profile; press New profile when you are ready."
        elif key in ("tab", "down"):
            m["field"] = (m["field"] + 1) % fields
        elif key in ("shift-tab", "up"):
            m["field"] = (m["field"] - 1) % fields
        elif m["field"] >= buttons_at:
            if key in ("left", "right"):
                m["field"] = buttons_at + (1 - (m["field"] - buttons_at))
            elif key in ("enter", " "):
                if m["field"] == buttons_at:
                    self._add_account() if m["type"] == "account" else self._add_profile()
                else:
                    self.modal = None
        elif m["type"] == "account" and m["field"] == 1:
            if key in ("left", "right", " ", "enter"):
                m["kind"] = (m["kind"] + (-1 if key == "left" else 1)) % len(KINDS)
        else:
            attr = {("account", 0): "name", ("account", 2): "secret", ("profile", 0): "name", ("profile", 1): "address"}[(m["type"], m["field"])]
            if key == "enter":
                m["field"] += 1
            elif key == "backspace":
                m[attr] = m[attr][:-1]
            elif key == "ctrl-u":
                m[attr] = ""
            elif len(key) == 1 and key.isprintable():
                m[attr] += key

    def _add_profile(self):
        m = self.modal
        name = m["name"].strip()
        if not profiles.valid_name(name):
            self.message, m["field"] = ("!A profile name starts with a letter or digit and uses letters, digits, spaces, dots, dashes and "
                                        "underscores (up to 40)."), 0
        elif name in self.entries:
            waiting = " that is waiting to be removed; save first" if self.entries[name].removed else ""
            self.message, m["field"] = f"!There is already a profile called {name}{waiting}.", 0
        elif profiles.load(name) is not None:
            self.message, m["field"] = f"!There is already a profile called {name}.", 0
        else:
            self.entries[name] = Entry(name, m["address"].strip(), creds_path=str(profiles.root() / name / profiles.CREDS), new=True)
            self.active, self.cursor, self.modal = name, 0, None
            self.message = f"Added the profile {name}. Add the admin accounts for it below; Save writes it all."

    def _add_account(self):
        m = self.modal
        name, secret = m["name"].strip(), m["secret"]
        if not name:
            self.message, m["field"] = "!The account needs a name (the PAN-OS user name).", 0
            return
        if not secret.strip():
            self.message, m["field"] = "!Type the " + ("API key." if m["kind"] == 1 else "password."), 2
            return
        if m["kind"] == 2:
            if not self.address.strip() or self.services is None:
                self.message = "!Signing in to obtain a key needs this profile's Panorama address; close this and type it in the Address field."
                return
            try:
                key = self.services.sign_in(self.address.strip(), name, secret)
            except CertificateProblem as problem:
                self.ask_about_certificate(problem, self._add_account)
                return
            except SourceError as exc:
                self.message = "!" + str(exc)
                return
            stored = {"api_key": key}
        else:
            stored = {"api_key": secret.strip()} if m["kind"] == 1 else secret
        replaced = name in self.accounts
        self.accounts[name] = stored
        self.cursor = self.account_names().index(name)
        self.modal = None
        self.message = f"{'Replaced' if replaced else 'Added'} {name}, signing in with " + (
            "a stored API key" if isinstance(stored, dict) else "its password") + " (written to the file when you save)."

    def _save(self):
        if self.services is None:
            self.message = "!Saving is not available here."
            return False
        try:
            self.message = self.services.save(self)
        except (OSError, ValueError, SourceError) as exc:
            self.message = f"!Could not save: {getattr(exc, 'strerror', None) or exc}"
            return False
        self.mark_saved()
        self.ever_saved = True
        return True

    # ---- drawing
    def render(self, width, height, title=""):
        """The screen as a list of lines; each line is a list of (style, text) pieces. Fits width x height. Long text wraps."""
        lines = [[("bold", f" {title}".rstrip() or " panupgrade auth")]]
        shown = PROMPT if self.prompt is not None else MODAL if self.modal is not None else self.zone
        lines += _wrapped("dim", HELP[shown], width)
        if height >= 24 and self.modal is None and self.prompt is None:
            lines += _wrapped("dim", ALWAYS, width)
        status = self.message
        lines += _wrapped("error" if status.startswith("!") else "dim", status.lstrip("!"), width) if status else [[]]
        lines.append([])
        names = self.names()
        quiet = self.modal is None and self.prompt is None
        lines += self._profile_lines(width, names, quiet) if quiet else []
        footer = self._buttons_line()
        if self.prompt is not None:
            lines += self._prompt_lines(width)
        elif self.modal is not None:
            lines += self._modal_lines(width)
        else:
            actions = self._actions_lines(width)
            room = max(height - len(lines) - len(actions) - 4, 2)
            lines += self._table(room)
            lines.append([])
            lines += actions
        lines = lines[:max(height - 2, 3)] + [[], footer]
        return [self._cut(line, width - 1) for line in lines]

    def _profile_lines(self, width, names, quiet):
        """The profile in use: which one, its address, its accounts file, the certificate trusted for it, and how it is remembered."""
        lines = []
        here = self.zone == PROFILE and quiet
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Panorama profile   "),
                      ("focus" if here else "", f"< {self.active or '(none yet)'} >"),
                      ("dim", f"  {names.index(self.active) + 1} of {len(names)}" if self.active in names else "")])
        here = self.zone == ADDRESS and quiet
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Address            "),
                      ("focus" if here else "", f"[{self.address}{'_' if here else ''}]".ljust(26)),
                      ("dim", "  this profile's Panorama")])
        if self.entry is not None:
            count = len(self.accounts)
            lines.append([("bold", "   Admin accounts     "), ("", f"{count} in {site_tilde(self.creds_path)}"),
                          ("dim", "  (not created yet)" if self.creds_exists is False and not count else "")])
        known = self.trusted()
        if known:
            lines.append([("dim", f"   Trusted certificate  SHA-256 {trust.pretty(known['sha256'])[:47]}...  (accepted {known.get('accepted', '?')})")])
        here = self.zone == STORE and quiet
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Profile in use is   "), ("focus" if here else "", f"< {STORES[self.store]} >")])
        for name in self.overrides:
            what = ACCOUNT_ENV.get(name)
            text = (f"This terminal has {name} set, which overrides the profile's choice of {what} until it is unset or you open a new terminal."
                    if what else f"This terminal has {name} set, which overrides the choice made here until it is unset or you open a new terminal.")
            lines += _wrapped("warn", text, width, "     ")
        lines.append([])
        return lines

    def _table(self, room):
        names = self.account_names()
        out = [[("bold", "     account" + " " * 24 + "signs in with        used for")]]
        if not names:
            return out + [[("dim", "     (none yet: choose Add account below)")]]
        start = max(0, min(self.cursor - room + 2, len(names) - (room - 1)))
        for i, name in enumerate(names[start:start + room - 1], start):
            on = self.zone == ACCOUNTS and i == self.cursor
            used = [w for w, n in (("reading", self.effective_reading()), ("changes", self.effective_changes())) if n == name]
            if len(self.accounts) == 1:
                used = ["reading and changes (the only account)"]
            out.append([("focus" if on else "", f" {'>' if on else ' '}   {name[:30].ljust(30)}   {credstore.kind(self.accounts[name]).ljust(18)}   "
                                                  f"{', '.join(used) or '-'}")])
        if len(names) > room - 1:
            out.append([("dim", f"     ({len(names)} accounts; the list scrolls)")])
        return out

    def _actions_lines(self, width):
        """The action buttons, wrapped onto as many lines as the window needs."""
        lines, line, used = [], [("", "   ")], 3
        for n, label in enumerate(ACTION_LABELS):
            piece = f"[ {label} ]"
            if used + len(piece) + 1 > width - 2 and used > 3:
                lines.append(line)
                line, used = [("", "   ")], 3
            on = self.zone == ACTIONS and self.action == n
            line += [("focus" if on else "bold", piece), ("", " ")]
            used += len(piece) + 1
        return lines + [line]

    def _buttons_line(self):
        line = [("", "   ")]
        for n, label in enumerate(BUTTON_LABELS):
            on = self.zone == BUTTONS and self.button == n
            line += [("focus" if on else "bold", f"[ {label} ]"), ("", "  ")]
        if self.dirty():
            line.append(("warn", "unsaved changes"))
        return line

    def _prompt_lines(self, width):
        prompt = self.prompt
        problem = prompt["problem"]
        out = [[("warn", "   Cannot verify Panorama's certificate")]]
        out += _wrapped("", f"{problem.address} presented a certificate that failed the normal check: {problem.reason}.", width, "     ")
        if problem.sha256:
            out.append([("", "     SHA-256 fingerprint:")])
            out += _wrapped("bold", trust.pretty(problem.sha256), width, "       ")
            out += _wrapped("dim", "Compare it with the fingerprint Panorama shows for its own certificate (Device > Certificate Management > "
                                   "Certificates) before accepting. Accepting makes this tool trust exactly this certificate for this address and "
                                   "refuse any other, so a replaced certificate or an interceptor is caught.", width, "     ")
        else:
            out += _wrapped("error", "Its certificate could not be read at all, so there is nothing to accept. Check the address and that "
                                     "Panorama is reachable.", width, "     ")
        accept_on, cancel_on = prompt["choice"] == 0, prompt["choice"] == 1
        out.append([("", "   "), ("focus" if accept_on else "bold" if problem.sha256 else "dim", "[ Accept risk & continue ]"), ("", "  "),
                    ("focus" if cancel_on else "bold", "[ Cancel ]")])
        return out

    def _modal_lines(self, width):
        m = self.modal

        def row(index, label, value):
            on = m["field"] == index
            return [("focus" if on else "bold", f" {'>' if on else ' '} {label.ljust(18)}"), ("focus" if on else "", value)]
        if m["type"] == "profile":
            out = [[("bold", "   New Panorama profile")]]
            out.append(row(0, "Profile name", f"[{m['name']}{'_' if m['field'] == 0 else ''}]".ljust(34)))
            out.append(row(1, "Panorama address", f"[{m['address']}{'_' if m['field'] == 1 else ''}]".ljust(34)))
            out += _wrapped("dim", "A profile is one Panorama with its own admin accounts, firewall list, settings and records, kept apart from "
                                   "every other Panorama.", width, "     ")
            buttons_at, labels = 2, ("Create", "Cancel")
        else:
            out = [[("bold", f"   Add an account to {self.active}")]]
            out.append(row(0, "Account name", f"[{m['name']}{'_' if m['field'] == 0 else ''}]".ljust(34)))
            out.append(row(1, "Signs in with", f"< {KINDS[m['kind']]} >"))
            out.append(row(2, "Secret (hidden)", f"[{'*' * len(m['secret'])}{'_' if m['field'] == 2 else ''}]".ljust(34)))
            out += _wrapped("dim", KIND_HELP[m["kind"]], width, "     ")
            buttons_at, labels = 3, ("Add", "Cancel")
        out.append([("", "   ")] + [(("focus" if m["field"] == buttons_at + n else "bold"), ("" if n == 0 else "  ") + f"[ {label} ]")
                                    for n, label in enumerate(labels)])
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


def site_tilde(path):
    """A path as the person would type it: under their home folder it starts with ~."""
    try:
        return os.path.join("~", str(Path(path).expanduser().resolve().relative_to(Path.home().resolve())))
    except ValueError:
        return str(path)


# ---- saving -----------------------------------------------------------------------------------------------------------------

def save_all(form, set_user_env=userenv.set, delete_user_env=userenv.delete):
    """Write what was staged: each profile's record and credentials file, and which profile is in use. Returns what was done.

    Only names, addresses and paths go in profile records and the environment variable; secrets go in a credentials file alone.
    A removed profile's folder is renamed, never deleted.
    """
    done = []
    for entry in list(form.entries.values()):
        if entry.removed:
            if not entry.new:
                done.append(f"removed {entry.name} (folder kept as {profiles.remove(entry.name)})")
            continue
        profile = profiles.load(entry.name) or profiles.Profile(entry.name)
        profile.address, profile.account, profile.work_account = entry.address, entry.reading, entry.changes
        if entry.creds_path != str(profile.dir / profiles.CREDS):
            profile.data["creds_file"] = entry.creds_path
        profile.save()
        if entry.accounts_changed() or entry.new or not Path(entry.creds_path).is_file():
            credstore.write(entry.creds_path, entry.accounts)
        if entry.state() != entry.saved:
            done.append(f"{entry.name} ({len(entry.accounts)} account{'s' if len(entry.accounts) != 1 else ''})")
    active = form.active if form.active in form.names() else ""
    if form.store == "env":
        (set_user_env("PANUPGRADE_PROFILE", active) if active else delete_user_env("PANUPGRADE_PROFILE"))
        profiles.remember("")
        done.append("the profile in use to PANUPGRADE_PROFILE (open a new terminal for it to take effect)")
    else:
        profiles.remember(active)
        if userenv.supported() and userenv.get("PANUPGRADE_PROFILE") is not None:
            delete_user_env("PANUPGRADE_PROFILE")                    # a leftover user-level variable would override the choice
    return "Saved " + (", ".join(done) if done else "nothing new") + "."
