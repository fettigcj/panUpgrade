# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The credentials file: named accounts, each holding a password or a ready-made API key.

It is a Python-style dict, one account per line::

    {
     'person': 'their-password',
     'service': {'api_key': 'KEY'},
    }

Only account names and the kind of secret ever leave this module for display; the secrets themselves go to the code that signs in
and nowhere else. This module reads and writes the file and never touches the network.
"""

import ast
import os
import tempfile
from pathlib import Path

from .sources import SourceError

PASSWORD, API_KEY = "password", "API key"


class NeedsSignIn(SourceError):
    """Signing in cannot start because something the person can fix is missing: no file, no accounts, or no account chosen."""


def is_secret(value):
    """A password (text), or an API key kept ready-made as {'api_key': 'KEY'}."""
    return (isinstance(value, str) and bool(value)) or (isinstance(value, dict) and set(value) == {"api_key"} and isinstance(value["api_key"], str)
                                      and bool(value["api_key"].strip()))


def kind(secret):
    return API_KEY if isinstance(secret, dict) else PASSWORD


def read(path):
    """The accounts in the file. Raises NeedsSignIn when there is no file; SourceError when it cannot be understood."""
    if not Path(path).is_file():
        raise NeedsSignIn(f"there is no credentials file at {path}. `panupgrade auth` creates one")
    try:
        data = ast.literal_eval(Path(path).read_text(encoding="utf-8").strip() or "{}")
    except (OSError, ValueError, SyntaxError) as exc:
        raise SourceError(f"cannot read credentials file {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and is_secret(v) for k, v in data.items()):
        raise SourceError(f"{path} must hold a dict of account name to password, or to {{'api_key': 'KEY'}}")
    return data


def write(path, accounts):
    """Write the accounts, replacing the file in one step. A new file is created readable by its owner only (where the system
    can say so); a folder that does not exist yet is created."""
    path = Path(path)
    for name, secret in accounts.items():
        if not isinstance(name, str) or not name.strip() or not is_secret(secret):
            raise ValueError("an account needs a name and a password or API key")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "{\n" + "".join(f" {name!r}: {secret!r},\n" for name, secret in accounts.items()) + "}\n"
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(body)
        if os.name != "nt":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
