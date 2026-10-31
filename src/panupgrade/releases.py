# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Which PAN-OS feature releases exist, in order, so "how many minor versions behind Panorama" can be counted.

Palo Alto's numbering is not regular: 10.2 was the first release to end in .2, 11.2 is followed by 12.1 (there is no 12.0), and
the next one may break another habit. So the distance between 12.2 and 11.2 is read off a table of the releases that really
exist (12.2, 12.1, 11.2: two steps), not worked out from the numbers.

The table ships with the tool and comes from the vendor's end-of-life summary page. It goes out of date as releases appear, so
``panupgrade releases --update`` downloads a fresh copy into a file you own (and may edit by hand: it is plain JSON). The
download itself lives in ``panupgrade_live.releasefeed``; this module never touches the network.
A release the table does not list yet is slotted in by its numbers, and the tool says so.
"""

import datetime
import json
import re
from html.parser import HTMLParser
from pathlib import Path

URL = "https://www.paloaltonetworks.com/services/support/end-of-life-announcements/end-of-life-summary"
TABLE_ID = "pan-os-panorama"                      # the id of the table on that page that lists PAN-OS & Panorama
BUNDLED = Path(__file__).resolve().parent / "data" / "pan-os-releases.json"
DEFAULT_FILE = "releases.json"
_RELEASE = re.compile(r"\d+\.\d+")


class ReleaseError(ValueError):
    pass


def key(release):
    """'11.2' -> (11, 2): ordered as numbers, so 10.10 sorts after 10.2."""
    major, minor = release.split(".")
    return int(major), int(minor)


class Releases:
    def __init__(self, entries, source="", fetched=""):
        self.entries = sorted(entries, key=lambda e: key(e["release"]))
        self.keys = [key(e["release"]) for e in self.entries]
        self.source, self.fetched = source, fetched

    def knows(self, version):
        return version.feature_key in self.keys

    def entry(self, version):
        return next((e for e in self.entries if key(e["release"]) == version.feature_key), None)

    def behind(self, panorama, firewall, unit):
        """How many ``unit`` ('major' or 'minor') the firewall's release is behind Panorama's. 0 when it is level or ahead.

        Minor counts steps along the releases that exist: with 11.2, 12.1 and 12.2 in the table, 11.2 is two behind 12.2.
        A release the table lacks is placed by its numbers, so an out-of-date table gives a close answer rather than none.
        """
        if firewall.feature_key >= panorama.feature_key:
            return 0
        if unit == "major":
            return max(panorama.major - firewall.major, 0)
        ordered = sorted(set(self.keys) | {panorama.feature_key, firewall.feature_key})
        return ordered.index(panorama.feature_key) - ordered.index(firewall.feature_key)

    def unlisted(self, *versions):
        """The feature releases among ``versions`` that the table does not list, as '12.3' text."""
        return sorted({v.feature for v in versions if not self.knows(v)})


def _validate(entries):
    if not isinstance(entries, list) or not entries:
        raise ReleaseError("the release table is empty")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("release"), str) or not _RELEASE.fullmatch(entry["release"]):
            raise ReleaseError(f"a release must look like '11.2', got {entry!r}")
    return entries


def load(path=None):
    """The table in a file, or the one that ships with the tool."""
    source = Path(path) if path else BUNDLED
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        entries = _validate(data["releases"])
    except FileNotFoundError:
        raise ReleaseError(f"there is no release table at {source}") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError(f"cannot read the release table {source}: {exc}") from None
    return Releases(entries, data.get("source", ""), data.get("fetched", ""))


_DEFAULT = []


def default():
    """The table that ships with the tool, read once."""
    if not _DEFAULT:
        _DEFAULT.append(load())
    return _DEFAULT[0]


def save(path, table):
    Path(path).write_text(json.dumps({"source": table.source, "fetched": table.fetched, "releases": table.entries}, indent=1) + "\n",
                          encoding="utf-8")


# ---- downloading the vendor's table ----------------------------------------------------------------------------------

class _Rows(HTMLParser):
    """The rows of the one table with the right id, as lists of cell text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._cells, self._in_table, self._depth, self._text = [], None, False, 0, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if dict(attrs).get("id") == TABLE_ID:
                self._in_table, self._depth = True, 0
            if self._in_table:
                self._depth += 1
        elif self._in_table and tag == "tr":
            self._cells = []
        elif self._in_table and tag in ("td", "th") and self._cells is not None:
            self._text = []

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            self._depth -= 1
            self._in_table = self._depth > 0
        elif self._in_table and tag in ("td", "th") and self._text is not None and self._cells is not None:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._text).replace("\xa0", " ")).strip())
            self._text = None
        elif self._in_table and tag == "tr" and self._cells is not None:
            self.rows.append(self._cells)
            self._cells = None

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def _date(text):
    """'July 30th, 2026' -> '2026-07-30'; 'May 2025' -> '2025-05-01'; anything else -> ''."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.replace("^", "").strip())
    for pattern in ("%B %d, %Y", "%B %Y"):
        try:
            return datetime.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def parse_eol_page(page):
    """The PAN-OS releases from the vendor's end-of-life page: release, release date, standard and extended end of life.

    Rows that are not a plain 'N.N' (such as the VM-Series-only 9.0-XFR branch) are left out: they are not upgrade steps.
    """
    rows = _Rows()
    rows.feed(page)
    found = []
    for cells in rows.rows:
        if len(cells) >= 3 and _RELEASE.fullmatch(cells[0]):
            later = [c for c in cells[3:] if c]
            found.append({"release": cells[0], "released": _date(cells[1]), "standard_eol": _date(cells[2]),
                          "extended_eol": _date(later[-1]) if later else ""})
    if len(found) < 5 or not any(e["release"] == "10.2" for e in found):
        raise ReleaseError("the page no longer looks like the PAN-OS end-of-life table (no rows, or 10.2 is missing); "
                           "the vendor probably changed its layout, so edit releases.json by hand for now")
    return found


def standing(entry, today=None):
    """'standard support', 'extended support' or 'end of life' for a table entry, as of today (the vendor's dates, nothing more)."""
    today = (today or datetime.date.today()).isoformat()
    if entry.get("standard_eol") and today <= entry["standard_eol"]:
        return "standard support"
    if entry.get("extended_eol") and today <= entry["extended_eol"]:
        return "extended support"
    return "end of life" if entry.get("standard_eol") else "unknown"
