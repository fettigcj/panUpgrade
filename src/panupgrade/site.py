# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Per-site defaults, taken from the environment so nothing about one installation is baked into the code."""

import os
from pathlib import Path

# Where credentials live and which accounts to use are the site's choice: set these environment variables once, or pass the flags.
DEFAULT_CREDS = os.environ.get("PANUPGRADE_CREDS") or str(Path.home() / ".panupgrade" / "credentials.txt")
DEFAULT_READ_ACCOUNT = os.environ.get("PANUPGRADE_READ_ACCOUNT", "panupgrade-read")
DEFAULT_WORK_ACCOUNT = os.environ.get("PANUPGRADE_WORK_ACCOUNT", "panupgrade-write")


def verify_tls():
    """Certificates are verified unless the operator opts out (a lab Panorama with a self-signed certificate)."""
    return os.environ.get("PANUPGRADE_INSECURE_TLS", "").lower() not in ("1", "true", "yes")
