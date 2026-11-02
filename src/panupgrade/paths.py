# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Work out which images to download and install to get from A to B.

Two ways to cross feature releases, both from Palo Alto's documentation (the knowledge-base article on base images, the "Determine the
Upgrade Path" pages, and the Skip Software Version Upgrade feature of PAN-OS 11.0):

* **skip** (from 10.1 on): install only the target. Up to three feature releases may be skipped. The target's base image and the base
  image of every release skipped over must be on the firewall (downloaded, never installed).
* **legacy** (below 10.1): step through every feature release: download its base image, install the latest preferred maintenance
  release of it, reboot. A path may be legacy up to 10.1 and then skip (10.0 -> 10.1 -> 11.1).

The base image of a feature release is the LOWEST version of it the firewall offers, without a hotfix: usually x.y.0, but a build that
was recalled is replaced by a later one (x.y.1), and only the firewall's own list says which. Within one feature release the path is
just the target.

Rules (``data/path_rules.json``)::

    {"verified": true,
     "skip": {"from": "10.1", "max_skipped": 3},
     "legacy": {"preferred": {"9.1": "9.1.19"},        # the maintenance release to install at each legacy stop (pinned: never guessed)
                "minimum_build": {"8.0": "8.0.20"}},   # a build the firewall must reach before it leaves that feature release
     "base_image": {"11.0": "11.0.1"}}                  # only when the firewall's own list is not available
"""

import json
from dataclasses import dataclass, field
from importlib import resources

from . import releases as releases_mod
from .versions import Version, VersionError


@dataclass
class PathResult:
    ok: bool
    steps: list = field(default_factory=list)   # [{"version", "download", "install"}]
    reason: str = ""
    kind: str = ""        # "no_data" | "not_applicable" when ok is False
    verified: bool = True


class _NoData(Exception):
    pass


def load_rules(path=None):
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    with resources.files("panupgrade").joinpath("data/path_rules.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def lowest_base_images(versions):
    """{feature release: its base image} from every version a firewall lists: the lowest one without a hotfix."""
    found = {}
    for text in versions or []:
        try:
            version = Version.parse(text)
        except VersionError:
            continue
        if version.hotfix:
            continue
        if version.feature not in found or version < Version.parse(found[version.feature]):
            found[version.feature] = str(version)
    return found


def base_for(feature, rules, bases=None):
    """The image every install in a feature release needs on the box first. ``bases`` is what the firewall itself offers (see
    ``lowest_base_images``); the rules' ``base_image`` and then x.y.0 are for when its list is not available."""
    if bases:
        if feature not in bases:
            raise _NoData(f"the firewall does not list any {feature} release, so there is no base image to download")
        return bases[feature]
    return (rules or {}).get("base_image", {}).get(feature) or f"{feature}.0"


def keep_images(rules, current, steps, target, bases=None):
    """The PAN-OS images a firewall must keep for this upgrade, as version strings: what is running and its base, the target and its
    base, and every image on the path with its base. Anything else on the disk is not needed."""
    keep = set()
    for text in [current, target] + [step["version"] for step in steps]:
        version = Version.parse(str(text))
        keep.add(str(version))
        try:
            keep.add(str(Version.parse(base_for(version.feature, rules, bases))))
        except _NoData:
            keep.add(str(version.base))
    return keep


def content_requirement(rules, steps):
    """The highest app-content number any installed step needs, as (number, version), or (None, None). A figure for the exact maintenance release (seen on a device) wins over the release-train floor from the release notes."""
    best = (None, None)
    for step in steps:
        if not step.get("install"):
            continue
        v = Version.parse(step["version"])
        need = (rules or {}).get("min_content", {}).get(f"{v.major}.{v.minor}.{v.maint}")
        if need is None:                                        # no release-specific figure: fall back to the release notes' floor for the train
            need = (rules or {}).get("min_content_feature", {}).get(f"{v.major}.{v.minor}")
        if need is not None and (best[0] is None or need > best[0]):
            best = (need, step["version"])
    return best


def _keys(features):
    return [tuple(int(n) for n in f.split(".")) for f in features]


def compute(current, target, rules, allow_downgrade=False, bases=None, releases=None):
    """The path from ``current`` to ``target``. ``bases``: what the firewall offers, as ``lowest_base_images`` gives it (None when not
    known). ``releases``: the table of feature releases that exist (the shipped one when None)."""
    cur, tgt = Version.parse(current), Version.parse(target)
    verified = bool(rules.get("verified"))
    if tgt == cur:
        return PathResult(False, reason=f"target {tgt} is what is already running", kind="not_applicable")
    if tgt < cur and not allow_downgrade:
        return PathResult(False, reason=f"target {tgt} is not newer than {cur}", kind="not_applicable")
    final = [{"version": str(tgt), "download": True, "install": True}]
    if cur.feature == tgt.feature:
        return PathResult(True, final, "same feature release: no intermediate needed" if tgt > cur else f"downgrade within feature release {cur.feature}")

    table = releases if releases is not None else releases_mod.default()
    ordered = sorted(set(table.keys) | {cur.feature_key, tgt.feature_key})
    at_of = {key: i for i, key in enumerate(ordered)}
    skip = rules.get("skip", {})
    skip_from = tuple(int(n) for n in skip.get("from", "10.1").split("."))
    max_skipped = int(skip.get("max_skipped", 3))
    name = lambda key: f"{key[0]}.{key[1]}"

    def base_step(key):
        return {"version": base_for(name(key), rules, bases), "download": True, "install": False}

    def between(a, b):
        lo, hi = sorted((at_of[a], at_of[b]))
        return ordered[lo + 1:hi]

    def target_steps(a):
        steps = [base_step(key) for key in between(a, tgt.feature_key)]
        if str(tgt) != base_for(tgt.feature, rules, bases):
            steps.append(base_step(tgt.feature_key))
        return steps + final

    try:
        if tgt < cur:                                         # going down: the same skip rule, in the other direction
            if min(cur.feature_key, tgt.feature_key) < skip_from or len(between(cur.feature_key, tgt.feature_key)) > max_skipped:
                return PathResult(False, reason=(f"downgrading from {cur.feature} to {tgt.feature} is outside what the skip rule covers "
                                                 f"(both releases {name(skip_from)} or later, at most {max_skipped} skipped); not guessing"),
                                  kind="no_data", verified=verified)
            steps = target_steps(cur.feature_key)
            return PathResult(True, steps, f"downgrade {cur.feature} -> {tgt.feature} by skipping {len(between(cur.feature_key, tgt.feature_key))} "
                                           f"release(s); the base images are downloaded, not installed", verified=False)

        legacy = rules.get("legacy", {})
        steps, here, here_version, hops = [], cur.feature_key, cur, []

        def install_preferred(key, have=None):
            feature = name(key)
            pinned = [Version.parse(v) for v in (legacy.get("preferred", {}).get(feature), legacy.get("minimum_build", {}).get(feature)) if v]
            if not legacy.get("preferred", {}).get(feature):
                raise _NoData(f"stepping through {feature} (below {name(skip_from)}) installs its latest preferred maintenance release, and the "
                              f"path rules do not name one for {feature} (legacy.preferred); not guessing")
            wanted = max(pinned)
            if have is None or have < wanted:
                steps.append({"version": str(wanted), "download": True, "install": True})
            return wanted

        if here < skip_from:                                  # leave the running release on its latest preferred build first
            install_preferred(here, have=cur)
        while True:
            skipped = len(between(here, tgt.feature_key))
            if skipped == 0 or (here >= skip_from and skipped <= max_skipped):
                break
            nxt = ordered[at_of[here] + 1] if here < skip_from else ordered[at_of[here] + max_skipped + 1]
            if nxt == tgt.feature_key:
                break
            steps += [base_step(key) for key in between(here, nxt)]
            steps.append(base_step(nxt))
            install_preferred(nxt)
            hops.append(name(nxt))
            here = nxt
        steps += target_steps(here)
        skipped_over = [name(k) for k in between(here, tgt.feature_key)]
        via = f"via {' -> '.join([cur.feature] + hops + [tgt.feature])}"
        reason = (f"{via}" + (f"; skips {', '.join(skipped_over)} (their base images are downloaded, not installed)" if skipped_over else ""))
        return PathResult(True, steps, reason, verified=verified)
    except _NoData as exc:
        return PathResult(False, reason=str(exc), kind="no_data", verified=verified)
