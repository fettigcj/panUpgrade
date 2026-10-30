# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""panupgrade: plan PAN-OS upgrades for a Panorama-managed fleet.

Nothing in this package opens a network connection or sends a command to a firewall or
Panorama; that is confined to the separate ``panupgrade_live`` package (a test enforces it).
"""

__version__ = "0.1.0.dev0"
