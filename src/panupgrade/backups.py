# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The configuration backups the tool writes, and clearing out the old ones.

Every backup step writes a new file named ``<hostname>_<serial>_<YYYYmmdd-HHMMSS>.xml`` into the profile's ``backups`` folder, so
they pile up. ``backup_keep_days`` (default 14) is how long they are worth keeping; once, when the tool is started, the person is
asked whether to delete the older ones. Nothing is deleted without that yes, and only files with exactly the tool's own naming
are ever considered: anything else in the folder is left alone.
"""

import re
import time
from pathlib import Path

OWN_NAME = re.compile(r"^.+_[0-9A-Za-z]+_\d{8}-\d{6}\.xml$")


def old_backups(directory, keep_days, now=None):
    """[(path, bytes)] of the tool's own backup files in ``directory`` last written more than ``keep_days`` days ago, oldest first.
    ``keep_days`` of 0 (or less) means never: the answer is empty. A missing folder is empty too."""
    if keep_days <= 0:
        return []
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    found = []
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            if path.is_symlink() or not path.is_file() or not OWN_NAME.match(path.name):
                continue
            info = path.stat()
        except OSError:
            continue
        if info.st_mtime < cutoff:
            found.append((path, info.st_size))
    return sorted(found, key=lambda item: (item[0].stat().st_mtime, item[0].name))


def size_text(total):
    return f"{total / 1048576:.1f} MB" if total >= 1048576 else f"{max(1, round(total / 1024))} KB"


def question(found, keep_days, directory):
    return (f"{len(found)} configuration backup{'s' if len(found) != 1 else ''} in {directory} {'are' if len(found) != 1 else 'is'} "
            f"older than {keep_days} days ({size_text(sum(size for _, size in found))}). Delete {'them' if len(found) != 1 else 'it'}?")


def delete(found):
    """Delete the files. Returns (how many went, [what could not be deleted and why])."""
    gone, problems = 0, []
    for path, _ in found:
        try:
            Path(path).unlink()
            gone += 1
        except OSError as exc:
            problems.append(f"{path.name}: {exc.strerror or exc}")
    return gone, problems
