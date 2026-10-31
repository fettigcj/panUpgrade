# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Panorama profiles: one workspace per Panorama, each carrying its own admin accounts.

A site with several Panoramas keeps them apart. A profile is a folder under ``~/.panupgrade/profiles/<name>/`` holding everything
that belongs to that Panorama and nothing that belongs to another::

    profile.json       its address, which account reads, which makes changes, the target version last used
    credentials.txt    the admin accounts for that Panorama (passwords or API keys; see ``credstore``)
    upgradeList.txt    the firewalls chosen for it
    settings.toml      its settings          releases.json    its PAN-OS release table
    runs/  prepared/  backups/               journals, upgrade kits and configuration backups

Which profile a command uses: ``--profile NAME``; else, when ``--panorama`` names an address, the profile with that address (a
Panorama that matches no profile is simply used as it is, with the current folder as its workspace); else the PANUPGRADE_PROFILE
environment variable; else the one used last, else the only one there is. Naming a Panorama beats the environment variable because it
is the more specific thing to have said.

Removing a profile never deletes anything: its folder is renamed ``<name>.removed-<date>`` and no longer listed, because it may hold
configuration backups and run journals.
"""

import json
import os
import re
from datetime import date
from pathlib import Path

from . import site

NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
FILE = "profile.json"
CREDS = "credentials.txt"


class ProfileError(ValueError):
    pass


def root():
    return site.home_dir() / "profiles"


class Profile:
    def __init__(self, name, data=None):
        self.name = name
        self.data = dict(data or {})

    @property
    def dir(self):
        return root() / self.name

    address = property(lambda self: self.data.get("address", ""), lambda self, v: self.data.__setitem__("address", v.strip()))
    account = property(lambda self: self.data.get("account", ""), lambda self, v: self.data.__setitem__("account", v))
    work_account = property(lambda self: self.data.get("work_account", ""), lambda self, v: self.data.__setitem__("work_account", v))
    target = property(lambda self: self.data.get("target", ""), lambda self, v: self.data.__setitem__("target", v.strip()))

    @property
    def creds_file(self):
        return self.data.get("creds_file") or str(self.dir / CREDS)

    def path(self, name):
        """A file or folder in this profile's workspace."""
        return self.dir / name

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / FILE).write_text(json.dumps({k: v for k, v in self.data.items() if v != ""}, indent=1) + "\n", encoding="utf-8")


def valid_name(name):
    return bool(NAME_OK.fullmatch(name or "")) and not name.endswith((" ", ".")) and "removed-" not in name


def names():
    """The profiles there are, in alphabetical order."""
    try:
        return sorted(p.name for p in root().iterdir() if (p / FILE).is_file() and ".removed-" not in p.name)
    except OSError:
        return []


def load(name):
    """The profile of that name, or None."""
    try:
        data = json.loads((root() / name / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Profile(name, data if isinstance(data, dict) else {})


def create(name, address=""):
    if not valid_name(name):
        raise ProfileError("a profile name starts with a letter or digit and may use letters, digits, spaces, dots, dashes and underscores "
                           "(up to 40 characters)")
    if load(name) is not None:
        raise ProfileError(f"there is already a profile called {name}")
    profile = Profile(name, {"address": address.strip()})
    profile.save()
    return profile


def remove(name):
    """Take a profile out of the list. Its folder is kept, renamed, so no backup or journal is lost. Returns the new folder name."""
    source = root() / name
    if not (source / FILE).is_file():
        raise ProfileError(f"there is no profile called {name}")
    stamp, n = date.today().strftime("%Y%m%d"), 1
    target = root() / f"{name}.removed-{stamp}"
    while target.exists():
        n += 1
        target = root() / f"{name}.removed-{stamp}-{n}"
    os.replace(source, target)
    return target.name


def remembered():
    return site.read_state().get("profile", "")


def remember(name):
    site.write_state(profile=name or "")


def choose(explicit=None, address=None, use_remembered=True):
    """The profile to use, by the rules in the module notes. Returns a Profile or None. An explicit name that does not exist is an error."""
    if explicit:
        profile = load(explicit)
        if profile is None:
            have = ", ".join(names()) or "none yet; `panupgrade auth` makes one"
            raise ProfileError(f"there is no profile called {explicit} (profiles: {have})")
        return profile
    if address:
        wanted = address.strip().lower()
        return next((p for p in map(load, names()) if p and p.address.lower() == wanted), None)
    named = os.environ.get("PANUPGRADE_PROFILE", "").strip()
    if named and load(named):
        return load(named)
    if use_remembered:
        last = load(remembered()) if remembered() else None
        if last:
            return last
        only = names()
        if len(only) == 1:
            return load(only[0])
    return None
