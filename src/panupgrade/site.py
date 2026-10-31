# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Per-site defaults, so nothing about one installation is baked into the code.

How people sign in is the site's business: one account for everything, a read-only account for the tool plus an
administrator's own login for changes, or an administrator's own login for all of it. The tool offers the pieces
(a credentials file with named accounts, and typing a login at the prompt) and assumes none of them.

Which credentials file and which account(s) to use come from, in order: the command line, an environment variable
(PANUPGRADE_CREDS, PANUPGRADE_ACCOUNT, PANUPGRADE_WORK_ACCOUNT), the Panorama profile in use (see ``profiles``), and finally the
defaults. Profiles and their settings hold names and paths only, never a secret.
"""

import json
import os
from pathlib import Path

CREDS_NAME = "credentials.txt"


def home_dir():
    """The folder that holds the profiles and what the shell remembers. PANUPGRADE_HOME moves it (tests use that)."""
    return Path(os.environ.get("PANUPGRADE_HOME") or Path.home() / ".panupgrade")


def default_creds_path():
    return home_dir() / CREDS_NAME


def creds_path(profile=None):
    """(path of the credentials file, where that choice came from)."""
    if os.environ.get("PANUPGRADE_CREDS"):
        return os.environ["PANUPGRADE_CREDS"], "PANUPGRADE_CREDS"
    if profile is not None:
        return profile.creds_file, f"profile:{profile.name}"
    return str(default_creds_path()), "default"


def _account(env_name, attr, profile):
    if os.environ.get(env_name):
        return os.environ[env_name], env_name
    value = getattr(profile, attr, "") if profile is not None else ""
    return (value, f"profile:{profile.name}") if value else (None, "default")


def account(profile=None):
    """The account to read with, when none is named on the command line: (name or None, where from)."""
    return _account("PANUPGRADE_ACCOUNT", "account", profile)


def work_account(profile=None):
    """The account for changes, when none is named: (name or None, where from)."""
    return _account("PANUPGRADE_WORK_ACCOUNT", "work_account", profile)


def read_state():
    """What the shell remembered last time (the profile in use, or the Panorama typed without one): plain words, nothing secret."""
    try:
        data = json.loads((home_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(**values):
    """Remember values for next time. Failing to is not worth interrupting anyone for."""
    state = {**read_state(), **{k: v for k, v in values.items() if isinstance(v, str)}}
    try:
        home_dir().mkdir(parents=True, exist_ok=True)
        (home_dir() / "state.json").write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


def verify_tls():
    """Certificates are verified unless the operator opts out (a lab Panorama with a self-signed certificate)."""
    return os.environ.get("PANUPGRADE_INSECURE_TLS", "").lower() not in ("1", "true", "yes")
