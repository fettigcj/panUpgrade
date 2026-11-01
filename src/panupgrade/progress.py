# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""What long work is doing right now, in a few words, for whoever is watching.

Code that waits on something (a Panorama call) says what it is waiting for with ``say``. Nothing listens at the command line, so
nothing happens; the one-screen shell registers a listener for the thread a background job runs in, and shows the latest words
beside that job. The listener belongs to the thread, so two jobs at once each show their own.
"""

import threading

_local = threading.local()


class Aborted(BaseException):
    """Raised, in the thread of read-only background work, by the next ``say`` after the person pressed Abort.

    It is a BaseException on purpose: the broad ``except Exception`` handlers around device work must not swallow it. It is never
    raised in work that changes firewalls (that stops at a step boundary instead), because it could land in the middle of a step."""


def listen(callback):
    """Send this thread's progress words to ``callback`` (or stop doing so, with None)."""
    _local.callback = callback


def say(text):
    callback = getattr(_local, "callback", None)
    if callback is not None:
        callback(text)
