# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The form behind ``panupgrade auth``: which accounts the tool can sign in with, which one reads, which one makes changes.

Accounts live in the credentials file (see ``credstore``). The choice of account is kept either in the sign-in settings file in
your home folder (the default) or in user-level environment variables (Windows). Passwords and API keys are typed hidden, go into
the credentials file and nowhere else, and are never shown again: the list shows only each account's name and what kind of secret
it holds.

``Form`` holds the state and knows nothing about terminals or the network. The things that need the network (obtaining an API key,
testing a sign-in) come in through ``services``, so this module can be tested by pressing keys at it.
"""

import copy
import json
import os
import textwrap

from . import credstore, site, trust, userenv
from .sources import SourceError
from .trust import CertificateProblem

ADDRESS, STORE, ACCOUNTS, ACTIONS, BUTTONS, ADD, PROMPT = "address", "store", "accounts", "actions", "buttons", "add", "prompt"
RING = [ADDRESS, STORE, ACCOUNTS, ACTIONS, BUTTONS]
ACTION_LABELS = ["Add account", "Remove", "Use for reading", "Use for changes", "Test sign-in", "Forget certificate"]
ADD_ACCOUNT, REMOVE, FOR_READING, FOR_CHANGES, TEST, FORGET = range(6)
BUTTON_LABELS = ["Save", "Save & Exit", "Quit without saving"]
SAVE_ONLY, SAVE_EXIT, QUIT = 0, 1, 2
STORES = {"config": "the sign-in settings file in your home folder", "env": "user environment variables (this Windows account)"}
KINDS = ["password", "API key", "password, then keep only the API key"]
KIND_HELP = [
    "The password is stored in the credentials file and used to sign in at the start of each run.",
    "A ready-made API key is stored in the credentials file and used as it is; no password is kept anywhere.",
    "You type the password once; the tool signs in to Panorama with it now, keeps only the API key it gets back, and forgets the password. "
    "Needs the Panorama address above. The account name must be the PAN-OS user name.",
]
HELP = {
    ADDRESS: "Type the Panorama address (used only to test a sign-in or obtain an API key).   Tab: next",
    STORE: "Left / Right: choose where your choice of account is kept.   Tab: next",
    ACCOUNTS: "Up / Down: choose an account.   A: add   Delete: remove   R: use for reading   C: use for changes   T: test   Tab: next",
    ACTIONS: "Left / Right: choose an action   Enter: do it   Tab: next",
    BUTTONS: "Left / Right: choose a button   Enter or Space: press it   Tab: next",
    ADD: "Tab: next field   Left / Right: change the kind   Enter: next / press   Esc: cancel adding",
    PROMPT: "Left / Right: choose   Enter: press it   Esc: do not trust it",
}
ALWAYS = "Ctrl-D = Save & Exit   Esc = Quit without saving (asks first if there are unsaved changes)"
ENV_NAMES = {"creds_file": "PANUPGRADE_CREDS", "account": "PANUPGRADE_ACCOUNT", "work_account": "PANUPGRADE_WORK_ACCOUNT"}


def _wrapped(style, text, width, indent=" "):
    return [[(style, line)] for line in textwrap.wrap(text, max(width - 2, 20), initial_indent=indent, subsequent_indent=indent,
                                                       break_on_hyphens=False)] or [[]]


class Form:
    def __init__(self, accounts, creds_path, creds_origin="", reading="", changes="", store="config", address="", notice="",
                 services=None, overrides=()):
        self.accounts = copy.deepcopy(accounts)
        self.creds_path, self.creds_origin = str(creds_path), creds_origin
        self.reading, self.changes, self.store, self.address = reading, changes, store, address
        self.saved = (copy.deepcopy(accounts), reading, changes, store)
        self.services, self.overrides = services, list(overrides)
        self.zone, self.cursor, self.action, self.button = ADDRESS if not address else ACCOUNTS, 0, ADD_ACCOUNT, SAVE_EXIT
        self.modal = None                                     # the add-an-account fields while they are open
        self.prompt = None                                    # "accept this certificate?" while it is being asked
        self.exit_after_trust = False                         # opened because a step hit a certificate problem: finish once it is settled
        self.message, self.armed = notice, False
        self.creds_exists = None                              # set by the caller: does the credentials file exist yet?
        self.ever_saved = False                               # True once a Save has worked, even if the form is quit afterwards

    # ---- state
    def names(self):
        return sorted(self.accounts)

    def current(self):
        names = self.names()
        return names[self.cursor] if names else None

    def effective_reading(self):
        return self.reading if self.reading in self.accounts else (self.names()[0] if len(self.accounts) == 1 else "")

    def effective_changes(self):
        return self.changes if self.changes in self.accounts else self.effective_reading()

    def dirty(self):
        return (self.accounts, self.reading, self.changes, self.store) != self.saved

    def accounts_changed(self):
        return self.accounts != self.saved[0]

    def mark_saved(self):
        self.saved = (copy.deepcopy(self.accounts), self.reading, self.changes, self.store)

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
        at = RING.index(self.zone)
        self.zone = RING[(at + delta) % len(RING)]

    def _text_key(self, key, attr):
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
            self.store = "env" if self.store == "config" else "config"
        elif key in ("left", "right", " ", "enter"):
            self.message = "!This system has no user-level environment variables to set, so the settings file is used."
        elif key == "down":
            self._step(1)
        elif key == "up":
            self._step(-1)

    def _accounts_key(self, key):
        names = self.names()
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
        elif key in ("delete",):
            self._do(REMOVE)
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
        if action == FORGET:
            self._forget()
        elif action == ADD_ACCOUNT:
            self.modal = {"field": 0, "name": "", "kind": 0, "secret": ""}
        elif name is None:
            self.message = "!There are no accounts yet. Add one first."
        elif action == REMOVE:
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
            self.message = (f"{name} will be used for changes." if self.changes else f"Changes will use the same account as reading.")
        elif action == TEST:
            self._test(name)

    def _test(self, name):
        if not self.address.strip():
            self.message = "!Type the Panorama address first (the first field)."
            self.zone = ADDRESS
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

    def _modal_key(self, key):
        m = self.modal
        fields = 5                                            # name, kind, secret, [Add], [Cancel]
        if key == "esc":
            self.modal = None
        elif key == "tab":
            m["field"] = (m["field"] + 1) % fields
        elif key == "shift-tab":
            m["field"] = (m["field"] - 1) % fields
        elif key == "up":
            m["field"] = (m["field"] - 1) % fields
        elif key == "down":
            m["field"] = (m["field"] + 1) % fields
        elif m["field"] in (0, 2):
            attr = "name" if m["field"] == 0 else "secret"
            if key == "enter":
                m["field"] += 1
            elif key == "backspace":
                m[attr] = m[attr][:-1]
            elif key == "ctrl-u":
                m[attr] = ""
            elif len(key) == 1 and key.isprintable():
                m[attr] += key
        elif m["field"] == 1:
            if key in ("left", "right", " ", "enter"):
                m["kind"] = (m["kind"] + (-1 if key == "left" else 1)) % len(KINDS)
        elif m["field"] == 3 and key in ("enter", " "):
            self._add()
        elif m["field"] == 4 and key in ("enter", " "):
            self.modal = None

    def _add(self):
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
                self.message = "!Signing in to obtain a key needs the Panorama address; close this and type it in the first field."
                return
            try:
                key = self.services.sign_in(self.address.strip(), name, secret)
            except CertificateProblem as problem:
                self.ask_about_certificate(problem, self._add)
                return
            except SourceError as exc:
                self.message = "!" + str(exc)
                return
            stored = {"api_key": key}
        else:
            stored = {"api_key": secret.strip()} if m["kind"] == 1 else secret
        replaced = name in self.accounts
        self.accounts[name] = stored
        self.cursor = self.names().index(name)
        self.modal = None
        self.message = f"{'Replaced' if replaced else 'Added'} {name}, signing in with " + (
            "a stored API key" if isinstance(stored, dict) else "its password") + " (written to the file when you save)."

    def trusted(self):
        """The certificate accepted for the address typed above (or None), for showing."""
        if not self.address.strip() or self.services is None or not hasattr(self.services, "trusted"):
            return None
        return self.services.trusted(self.address.strip())

    def _forget(self):
        if not self.address.strip():
            self.message, self.zone = "!Type the Panorama address first (the first field).", ADDRESS
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
        lines += _wrapped("dim", HELP[PROMPT if self.prompt is not None else ADD if self.modal is not None else self.zone], width)
        if height >= 24 and self.modal is None and self.prompt is None:
            lines += _wrapped("dim", ALWAYS, width)
        status = self.message
        lines += _wrapped("error" if status.startswith("!") else "dim", status.lstrip("!"), width) if status else [[]]
        lines.append([])
        here = self.zone == ADDRESS and self.modal is None
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Panorama address  "),
                      ("focus" if here else "", f"[{self.address}{'_' if here else ''}]".ljust(26)),
                      ("dim", "  for testing a sign-in or obtaining an API key")])
        known = self.trusted()
        if known:
            lines.append([("dim", f"   Trusted certificate  SHA-256 {trust.pretty(known['sha256'])[:47]}...  (accepted {known.get('accepted', '?')})")])
        count = len(self.accounts)
        lines.append([("bold", "   Credentials file   "), ("", site_tilde(self.creds_path)),
                      ("dim", f"  ({self.creds_origin}; " + ("not created yet" if self.creds_exists is False else
                                                              f"{count} account{'s' if count != 1 else ''}") + ")")])
        here = self.zone == STORE and self.modal is None
        lines.append([("focus" if here else "bold", f" {'>' if here else ' '} Keep my choices in   "),
                      ("focus" if here else "", f"< {STORES[self.store]} >")])
        for name in self.overrides:
            lines += _wrapped("warn", f"This terminal has {name} set, which overrides the choice made here until it is unset or you open a new "
                                      f"terminal.", width, "     ")
        lines.append([])
        footer = self._buttons_line()
        if self.prompt is not None:
            lines += self._prompt_lines(width)
        elif self.modal is not None:
            lines += self._modal_lines(width)
        else:
            room = max(height - len(lines) - 6, 2)
            lines += self._table(room)
            lines.append([])
            lines.append(self._actions_line())
        lines.append([])
        lines.append(footer)
        return [self._cut(line, width - 1) for line in lines[:height]]

    def _table(self, room):
        names = self.names()
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

    def _actions_line(self):
        line = [("", "   ")]
        for n, label in enumerate(ACTION_LABELS):
            on = self.zone == ACTIONS and self.action == n
            line += [("focus" if on else "bold", f"[ {label} ]"), ("", " ")]
        return line

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
        out = [[("bold", "   Add an account")]]

        def row(index, label, value):
            on = m["field"] == index
            return [("focus" if on else "bold", f" {'>' if on else ' '} {label.ljust(18)}"), ("focus" if on else "", value)]
        out.append(row(0, "Account name", f"[{m['name']}{'_' if m['field'] == 0 else ''}]".ljust(34)))
        out.append(row(1, "Signs in with", f"< {KINDS[m['kind']]} >"))
        out.append(row(2, "Secret (hidden)", f"[{'*' * len(m['secret'])}{'_' if m['field'] == 2 else ''}]".ljust(34)))
        out += _wrapped("dim", KIND_HELP[m["kind"]], width, "     ")
        out.append([("", "   ")] + [(("focus" if m["field"] == 3 + n else "bold"), f"[ {label} ]") if n == 0 else
                                    (("focus" if m["field"] == 3 + n else "bold"), f"  [ {label} ]") for n, label in enumerate(("Add", "Cancel"))])
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
    from pathlib import Path
    try:
        return os.path.join("~", str(Path(path).expanduser().resolve().relative_to(Path.home().resolve())))
    except ValueError:
        return str(path)


# ---- saving -----------------------------------------------------------------------------------------------------------------

def save_all(form, set_user_env=userenv.set, delete_user_env=userenv.delete):
    """Write the credentials file (when accounts changed) and keep the choice of account where the form says. Returns what was done.

    Only names and paths go in the sign-in settings and the environment variables; secrets go in the credentials file alone.
    """
    done = []
    if form.accounts_changed() or not os.path.isfile(form.creds_path):
        credstore.write(form.creds_path, form.accounts)
        done.append(f"{len(form.accounts)} account{'s' if len(form.accounts) != 1 else ''} to {site_tilde(form.creds_path)}")
    chosen = {"account": form.reading, "work_account": form.changes}
    config = site.read_config()
    if form.store == "env":
        for key, value in chosen.items():
            (set_user_env(ENV_NAMES[key], value) if value else delete_user_env(ENV_NAMES[key]))
            config.pop(key, None)
        done.append("your choice of account to user environment variables (open a new terminal for it to take effect)")
    else:
        for key, value in chosen.items():
            if value:
                config[key] = value
            else:
                config.pop(key, None)
            if userenv.supported() and userenv.get(ENV_NAMES[key]) is not None:
                delete_user_env(ENV_NAMES[key])                   # a leftover user-level variable would override the file
        done.append(f"your choice of account to {site_tilde(site.config_path())}")
    path = site.config_path()
    if config:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return "Saved " + " and ".join(done) + "."
