# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Certificate-expiry advisories: is this PAN-OS build (and content) one that carries the fix?

Older builds ship certificates that have expired (the root/default certificate on 31 Dec 2023, the CDSS and Panorama
certificates from 7 Apr 2024). A firewall on such a build misbehaves in ways that look unrelated: Panorama and cloud
connections reject certificates as "expired", HA peer details read Unknown, config syncs are refused. The cure is a build
that contains the fix, or, for the second advisory, fixed content AND a restart after it. The tables live in
``data/cert_advisories.json`` and are UNVERIFIED against the advisories themselves (see the file).
"""

import json
from importlib import resources

from .versions import Version, VersionError


def load(path=None):
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    with resources.files("panupgrade").joinpath("data/cert_advisories.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _content_key(text):
    """'8795-8489' -> (8795, 8489); None when it is not a content version ('0', 'Unknown', empty)."""
    try:
        a, b = text.split("-", 1)
        return int(a), int(b)
    except (AttributeError, ValueError):
        return None


def software_fixed(advisory, version):
    """True / False for a build the table covers; the whole train is 'affected' when the table says nothing about it."""
    v = version if isinstance(version, Version) else Version.parse(version)
    newest = max(Version.parse(f"{t}.0").feature_key for t in advisory["fixed"])
    if v.feature_key > newest:
        return True                  # a release train newer than the advisory's tables shipped after the fix
    for op, text in advisory["fixed"].get(v.feature, []):
        rule = Version.parse(text)
        if op == ">=" and v >= rule:
            return True
        if op == "==" and v.feature_key == rule.feature_key and v.maint == rule.maint and v.hotfix >= rule.hotfix:
            return True
    return False


def content_fixed(advisory, installed):
    got, need = _content_key(installed), _content_key(advisory["fixed_content"])
    return got is not None and got >= need


def assess(advisories, running, content, target):
    """What the advisories say about a firewall now (running, content) and after the upgrade (target).

    Returns {"now": [(advisory, state)], "target_affected": [advisory titles]} where state is one of
    'fixed' | 'needs_restart' | 'affected'.
    """
    now = []
    for adv in advisories:
        if software_fixed(adv, running):
            now.append((adv, "fixed"))
        elif content_fixed(adv, content):
            now.append((adv, "needs_restart" if adv.get("needs_restart_after_content") else "fixed"))
        else:
            now.append((adv, "affected"))
    return {"now": now, "target_affected": [a["title"] for a in advisories if not software_fixed(a, target)]}


__all__ = ["load", "software_fixed", "content_fixed", "assess", "VersionError"]
