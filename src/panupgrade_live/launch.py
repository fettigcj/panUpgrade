"""Start the background worker: the one place the tool starts a process of its own."""

import os
import subprocess


def spawn(argv, log_path, env=None):
    """Start ``argv`` detached from this terminal: its own session (POSIX) or process group without a console (Windows), no input, its
    output appended to ``log_path``. Returns the Popen (kept by a viewer that started it, so it can tell when the worker ends)."""
    log = open(log_path, "ab")
    options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True, "env": env}
    if os.name == "nt":
        options["creationflags"] = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **options)
    finally:
        log.close()
