# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Per-site defaults, so nothing about one installation is baked into the code.

How people sign in is the site's business: one account for everything, a read-only account for the tool plus an
administrator's own login for changes, or an administrator's own login for all of it. The tool offers the pieces
(a credentials file with named accounts, and typing a login at the prompt) and assumes none of them.

Which credentials file and which account(s) to use come from, in order: the command line, an environment variable
(PANUPGRADE_CREDS, PANUPGRADE_ACCOUNT, PANUPGRADE_WORK_ACCOUNT), the sign-in settings file ``~/.panupgrade/auth.json``
that ``panupgrade auth`` writes, and finally the defaults. The settings hold names and paths only, never a secret.
"""

import json
import os
from pathlib import Path

CONFIG_NAME = "auth.json"
CREDS_NAME = "credentials.txt"


def home_dir():
    """The folder that holds the sign-in settings and, by default, the credentials file. PANUPGRADE_HOME moves it (tests use that)."""
    return Path(os.environ.get("PANUPGRADE_HOME") or Path.home() / ".panupgrade")


def config_path():
    return home_dir() / CONFIG_NAME


def default_creds_path():
    return home_dir() / CREDS_NAME


def read_config():
    """The sign-in settings file as a dict. A missing or unreadable file is just no settings."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick(env_name, key, default=None):
    """(value, where it came from): the environment variable, else the settings file, else the default."""
    if os.environ.get(env_name):
        return os.environ[env_name], env_name
    value = read_config().get(key)
    if isinstance(value, str) and value:
        return value, CONFIG_NAME
    return default, "default"


def creds_path():
    return _pick("PANUPGRADE_CREDS", "creds_file", str(default_creds_path()))


def account():
    """The account to read with, when none is named on the command line: (name or None, where from)."""
    return _pick("PANUPGRADE_ACCOUNT", "account")


def work_account():
    """The account for changes, when none is named: (name or None, where from)."""
    return _pick("PANUPGRADE_WORK_ACCOUNT", "work_account")


def read_state():
    """What the shell remembered last time (the Panorama address and target version): plain words, nothing secret."""
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
