# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""`panupgrade probe-config`: read-only. Shows, for ONE firewall, the exact XML of the settings the asymmetry window and the load-balancer drain
edit, as each configuration view of the firewall presents them: running, candidate, merged, pushed-template, and the diff between candidate and
running. It exists because settings that a Panorama template pushes are not in the firewall's local XML the way a local setting is, and the edit and
restore logic has to be built from how they really look (attributes such as the template a node came from included), not from a guess.

Only ``show`` commands are sent. Nothing is changed on any firewall."""

import xml.etree.ElementTree as ET

VIEWS = ("running", "candidate", "merged", "pushed-template", "diff")

# What is looked for: the tails of an element's path (names of its ancestors; entries are shown with their name).
_SETTING_TAILS = (("deviceconfig", "setting", "tcp"), ("deviceconfig", "setting", "session"))
_PROFILE_LIST = ("network", "profiles", "interface-management-profile")


def command_for(view):
    return f"show config {view}"


def _label(element):
    name = element.get("name")
    return f"{element.tag}[{name}]" if name is not None else element.tag


def extract(root):
    """[(path, xml text)] for every piece of interest in one reply: the TCP and session settings under deviceconfig, the whole list of interface
    management profiles, and every interface that names a profile (its path says which interface). The XML keeps all attributes."""
    found = []

    def walk(element, path):
        here = path + [element.tag]
        names = tuple(here)
        if names[-3:] in _SETTING_TAILS or names[-3:] == _PROFILE_LIST:
            found.append(("/".join(path + [_label(element)]), ET.tostring(element, encoding="unicode").strip()))
            return
        if element.tag == "interface-management-profile" and "network" in names and "profiles" not in names:
            found.append(("/".join(path + [_label(element)]), ET.tostring(element, encoding="unicode").strip()))
            return
        for child in element:
            walk(child, path + [_label(element)])
    walk(root, [])
    return found


def probe(op, views=VIEWS):
    """The report text. ``op(command)`` returns a parsed XML reply or raises; a view the firewall refuses is reported, not hidden."""
    out = []
    for view in views:
        command = command_for(view)
        out.append(f"===== {command} =====")
        try:
            reply = op(command)
        except Exception as exc:  # a refusal is itself the finding
            out += [f"(the firewall or Panorama refused or failed: {type(exc).__name__}: {exc})", ""]
            continue
        status = reply.get("status")
        size = len(ET.tostring(reply))
        pieces = extract(reply)
        out.append(f"(reply status {status!r}, {size} bytes, {len(pieces)} piece(s) of interest)")
        if not pieces:
            text = " ".join((e.text or "").strip() for e in reply.iter("line"))
            out.append(f"(none of the settings appear in this view{'; the reply says: ' + text if text else ''})")
        for path, xml in pieces:
            out += [f"--- {path}", xml]
        out.append("")
    return "\n".join(out) + "\n"
