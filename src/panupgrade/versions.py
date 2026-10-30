# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""PAN-OS version strings: parse, order, and format.

Order is (major, minor, maintenance, hotfix); "20.2.9" < "20.2.9-h1" < "20.2.10".
Anything that does not fit the shape raises VersionError so callers can report
"no data" instead of guessing (the old script crashed or mis-split on odd strings).
"""

import re
from dataclasses import dataclass

_SHAPE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-h(\d+))?$")


class VersionError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    maint: int
    hotfix: int = 0

    @classmethod
    def parse(cls, text):
        if not isinstance(text, str):
            raise VersionError(f"version must be text, got {type(text).__name__}")
        match = _SHAPE.match(text.strip())
        if not match:
            raise VersionError(f"unrecognised PAN-OS version {text!r}")
        major, minor, maint, hotfix = match.groups()
        return cls(int(major), int(minor), int(maint), int(hotfix or 0))

    @property
    def feature(self):
        """The feature release, e.g. '20.2'."""
        return f"{self.major}.{self.minor}"

    @property
    def feature_key(self):
        """The feature release as numbers, for ordering: (10, 10) sorts after (10, 2); the text '10.10' would not."""
        return (self.major, self.minor)

    @property
    def base(self):
        """The first build of this feature release, e.g. 20.2.0 (the image a hop must have downloaded)."""
        return Version(self.major, self.minor, 0)

    def __str__(self):
        base = f"{self.major}.{self.minor}.{self.maint}"
        return f"{base}-h{self.hotfix}" if self.hotfix else base
