# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Per-site defaults, taken from the environment so nothing about one installation is baked into the code.

How people sign in is the site's business: one account for everything, a read-only account for the tool plus an
administrator's own login for changes, or an administrator's own login for all of it. The tool offers the pieces
(a credentials file with named accounts, and typing a login at the prompt) and assumes none of them.
"""

import os
from pathlib import Path

DEFAULT_CREDS = os.environ.get("PANUPGRADE_CREDS") or str(Path.home() / ".panupgrade" / "credentials.txt")
DEFAULT_ACCOUNT = os.environ.get("PANUPGRADE_ACCOUNT")       # None: decided from the file
DEFAULT_WORK_ACCOUNT = os.environ.get("PANUPGRADE_WORK_ACCOUNT")                                          # None: same as the account above


def verify_tls():
    """Certificates are verified unless the operator opts out (a lab Panorama with a self-signed certificate)."""
    return os.environ.get("PANUPGRADE_INSECURE_TLS", "").lower() not in ("1", "true", "yes")
