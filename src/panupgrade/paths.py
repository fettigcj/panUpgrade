"""Work out which releases to download and install to get from A to B.

The rules about *which feature releases may be skipped* are data
(``data/path_rules.json``), not code, and the shipped file is empty and marked
unverified. So:

* within one feature release the path is just the target (needs no rules);
* across feature releases the engine only answers when the rules data says how,
  otherwise it answers "no data". It never guesses a path.

Rules format::

    {"verified": true|false,
     "direct_from": {"21.0": ["20.2", "20.1"]},   # target feature -> feature releases it may be reached from
     "stepping_install": {"20.2": "20.2.3"},      # optional: version to install when 20.2 is only a stepping stone
     "min_source": {"21.0": {"20.2": "20.2.3"}}}  # optional: to hop 20.2 -> 21.0 the firewall must already run >= 20.2.3
"""

import json
from collections import deque
from dataclasses import dataclass, field
from importlib import resources

from .versions import Version


@dataclass
class PathResult:
    ok: bool
    steps: list = field(default_factory=list)   # [{"version", "download", "install"}]
    reason: str = ""
    kind: str = ""        # "no_data" | "not_applicable" when ok is False
    verified: bool = True


def load_rules(path=None):
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    with resources.files("panupgrade").joinpath("data/path_rules.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _base(feature):
    return f"{feature}.0"


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


def compute(current, target, rules, allow_downgrade=False, allow_cross_feature_downgrade=False):
    cur, tgt = Version.parse(current), Version.parse(target)
    verified = bool(rules.get("verified"))
    if tgt == cur:
        return PathResult(False, reason=f"target {tgt} is what is already running", kind="not_applicable")
    if tgt < cur:
        if not allow_downgrade:
            return PathResult(False, reason=f"target {tgt} is not newer than {cur}", kind="not_applicable")
        if cur.feature != tgt.feature:
            if not allow_cross_feature_downgrade:
                return PathResult(False, reason=f"downgrading across feature releases ({cur.feature} to {tgt.feature}) needs "
                                                f"allow_cross_feature_downgrade = true", kind="not_applicable")
            steps = []
            if tgt != tgt.base:      # the older feature release's base image has to be on the box first, as for an upgrade
                steps.append({"version": str(tgt.base), "download": True, "install": False})
            steps.append({"version": str(tgt), "download": True, "install": True})
            return PathResult(True, steps, f"downgrade across feature releases {cur.feature} -> {tgt.feature} (override setting is on)",
                              verified=False)
        return PathResult(True, [{"version": str(tgt), "download": True, "install": True}],
                          f"downgrade within feature release {cur.feature}", verified=True)
    if cur.feature == tgt.feature:
        steps = [{"version": str(tgt), "download": True, "install": True}]
        return PathResult(True, steps, "same feature release: no intermediate needed", verified=True)

    direct = rules.get("direct_from", {})
    # breadth-first over feature releases, shortest hop count first
    queue, seen = deque([(cur.feature, [])]), {cur.feature}
    hops = None
    while queue:
        feature, trail = queue.popleft()
        if feature == tgt.feature:
            hops = trail
            break
        for nxt, sources in direct.items():
            if feature in sources and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, trail + [nxt]))
    if hops is None:
        return PathResult(False, reason=(
            f"no path rule from {cur.feature} to {tgt.feature} in the path rules data "
            f"(verified={verified}); not guessing"), kind="no_data", verified=verified)

    stepping = rules.get("stepping_install", {})
    min_source = rules.get("min_source", {})
    features = [cur.feature] + hops
    steps = []
    for i, nxt in enumerate(hops):   # the hop from features[i] into nxt
        need = min_source.get(nxt, {}).get(features[i])
        need = Version.parse(need) if need else None
        if i == 0:
            if need and cur < need:   # must first be on a new enough build of the feature release it is in
                steps.append({"version": str(need), "download": True, "install": True})
        else:
            version = Version.parse(stepping.get(features[i], _base(features[i])))
            if need and version < need:
                version = need
            steps.append({"version": str(version), "download": True, "install": True})
    if str(tgt) != _base(tgt.feature):
        steps.append({"version": _base(tgt.feature), "download": True, "install": False})
    steps.append({"version": str(tgt), "download": True, "install": True})
    return PathResult(True, steps, f"via {' -> '.join([cur.feature] + hops)}", verified=verified)
