# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""User-level environment variables: the ones every new terminal of this user starts with.

Only Windows has such a place (the user's own registry key, the one the Environment Variables dialog edits). Other systems
read their variables from shell start-up files, which this tool does not edit, so ``supported()`` is False there and the
sign-in settings file is used instead. Changing a variable here does not change the terminal you are in; open a new one.
"""

import os

KEY = "Environment"


def supported():
    return os.name == "nt"


def get(name):
    """The user-level value of ``name``, or None."""
    if not supported():
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def set(name, value):
    """Set ``name`` for this user, for terminals opened from now on."""
    if not supported():
        raise OSError("this system has no user-level environment variables to set")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _announce()


def delete(name):
    """Remove ``name`` from the user-level variables. Not having it is fine."""
    if not supported():
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        return
    _announce()


def _announce():
    """Tell running programs (Explorer, new terminals) that the environment changed."""
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))
    except Exception:
        pass
