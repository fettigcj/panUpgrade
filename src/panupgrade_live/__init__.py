# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The only place that talks to a device, and only to read.

Kept apart from ``panupgrade`` on purpose: the engine package is forbidden from importing anything that can
reach a network (a test enforces it), so everything that can is here, small and auditable. Read-only is enforced
in ``PanoramaApi.op`` by the same prefix list the offline code uses; there is no config, commit or request path.
"""
