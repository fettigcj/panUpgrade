# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The terminal layer shared by the full-screen forms (``select``, ``settings``): draw a form, read keys, restore the terminal.

A form is any object with ``render(width, height, title)`` returning lines, each a list of ``(style, text)`` pieces, and
``press(key)`` returning ``'accept'``, ``'cancel'`` or None. Standard library only: console key events on Windows,
``termios`` elsewhere.
"""

import os
import shutil
import sys


def plain(lines):
    """The screen as plain text, for tests and for people who want to see it without colour."""
    return "\n".join("".join(text for _style, text in line) for line in lines)


CODES = {"bold": "\x1b[1m", "dim": "\x1b[2m", "focus": "\x1b[7m", "error": "\x1b[31m", "warn": "\x1b[33m", "focus-error": "\x1b[7;31m", "": ""}


def draw(form, out, width, height, title=""):
    parts = ["\x1b[H"]
    for line in form.render(width, height, title):
        parts.append("".join(f"{CODES[style]}{text}\x1b[0m" if style else text for style, text in line) + "\x1b[K\r\n")
    parts.append("\x1b[J")
    out.write("".join(parts))
    out.flush()


def run(form, read_key, out, size, title=""):
    """Show the form and feed it keys until it finishes. ``size()`` gives (width, height) and is re-read on every key so a
    resized window is followed. Returns True when accepted, False when cancelled."""
    while True:
        width, height = size()
        draw(form, out, width, height, title)
        result = form.press(read_key())
        if result:
            return result == "accept"


# ---- the real terminal ------------------------------------------------------------------------------------------------
_ANSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "Z": "shift-tab", "H": "home", "F": "end",
              "5~": "pgup", "6~": "pgdn", "1~": "home", "7~": "home", "4~": "end", "8~": "end", "3~": "delete"}
_PLAIN_KEYS = {"\r": "enter", "\n": "enter", "\t": "tab", "\x7f": "backspace", "\x08": "backspace", "\x15": "ctrl-u", "\x03": "ctrl-c",
               "\x04": "ctrl-d",
               "\x12": "ctrl-r"}


_VK_KEYS = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup", 0x22: "pgdn", 0x24: "home", 0x23: "end",
            0x2E: "delete", 0x0D: "enter", 0x1B: "esc", 0x08: "backspace"}
_SHIFT = 0x10


def translate_windows_key(vk, char, state):
    """One Windows console key event as a key name. Reading the event itself (not msvcrt) is what reveals Shift on Tab."""
    if vk == 0x09:
        return "shift-tab" if state & _SHIFT else "tab"
    if vk in _VK_KEYS:
        return _VK_KEYS[vk]
    if char and char != chr(0):
        return _PLAIN_KEYS.get(char, char)
    return ""                                   # Shift, Ctrl, Alt and the like on their own


def _read_key_windows():
    import ctypes

    class KeyEvent(ctypes.Structure):
        _fields_ = [("down", ctypes.c_int32), ("repeat", ctypes.c_uint16), ("vk", ctypes.c_uint16), ("scan", ctypes.c_uint16),
                    ("char", ctypes.c_wchar), ("state", ctypes.c_uint32)]

    class Union(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("pad", ctypes.c_byte * 16)]

    class Record(ctypes.Structure):
        _fields_ = [("kind", ctypes.c_uint16), ("event", Union)]

    kernel = ctypes.windll.kernel32
    handle = kernel.GetStdHandle(-10)
    record, count = Record(), ctypes.c_uint32()
    while True:
        if not kernel.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(count)):
            raise OSError("cannot read keys from this console")
        key = record.event.key
        if record.kind == 1 and key.down:                # a key going down; ignore releases, mouse and resize events
            name = translate_windows_key(key.vk, key.char, key.state)
            if name:
                return name


def _read_key_posix(fd):
    import select
    first = os.read(fd, 1)
    if first == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        if os.read(fd, 1) not in (b"[", b"O"):
            return ""
        seq = ""
        while select.select([fd], [], [], 0.05)[0]:
            tail = os.read(fd, 1).decode("ascii", "replace")
            seq += tail
            if tail.isalpha() or tail == "~":
                break
        return _ANSI_KEYS.get(seq, "")
    ch = first
    if first[0] >= 0xC0:
        ch += os.read(fd, 1 if first[0] < 0xE0 else 2 if first[0] < 0xF0 else 3)
    ch = ch.decode("utf-8", "replace")
    return _PLAIN_KEYS.get(ch, ch)


def _enable_vt_on_windows():
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def show(form, title=""):
    """Run the form on the real terminal. Returns True when accepted, False when cancelled."""
    out = sys.stdout
    if os.name == "nt":
        _enable_vt_on_windows()
        read_key, restore = _read_key_windows, lambda: None
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        read_key = lambda: _read_key_posix(fd)   # noqa: E731
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)   # noqa: E731
    out.write("\x1b[?1049h\x1b[?25l")        # a separate screen, so the user's scrollback is left alone; hide the cursor
    try:
        return run(form, read_key, out, lambda: tuple(shutil.get_terminal_size((100, 30))), title)
    except KeyboardInterrupt:
        return False
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
        restore()
