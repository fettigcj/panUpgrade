# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Certificates a person has chosen to trust, pinned by fingerprint.

Panorama (and every firewall behind it) usually presents a certificate its own CA made, so the normal check ("is this signed by a
CA my system trusts?") fails. Turning the check off for good is the wrong cure: anyone on the path could then pose as Panorama and
collect the password. Instead, once someone has looked at the SHA-256 fingerprint and accepted it, this module remembers exactly
that certificate for that address. Later connections must present the same certificate (checked in ``panupgrade_live``); a different
one is refused loudly, because it means either a replaced certificate or someone in the middle.

The record holds addresses and fingerprints, which are public. It is a file in the sign-in folder; nothing secret goes in it.
"""

import json
from datetime import date

from . import site
from .sources import SourceError


class CertificateProblem(SourceError):
    """Panorama's certificate cannot be verified, and the person has not accepted it. Carries what is needed to decide: the
    reason the system gave and the certificate's SHA-256 fingerprint (None when it could not be read at all)."""

    def __init__(self, address, reason, sha256=None):
        self.address, self.reason, self.sha256 = address, reason, sha256
        super().__init__(f"{address}: Panorama's certificate cannot be verified ({reason}). Accept it once in `panupgrade auth` "
                         f"(Test sign-in), or when asked at the terminal, and it is trusted from then on")


class CertificateChanged(SourceError):
    """The address has an accepted certificate and this is a different one."""

    def __init__(self, address, expected, seen):
        self.address, self.expected, self.seen = address, expected, seen
        super().__init__(f"{address}: the certificate is not the one you accepted before (accepted {pretty(expected)}, now {pretty(seen)}). "
                         f"Either Panorama's certificate was replaced or someone is intercepting the connection. If the change is expected, "
                         f"forget the old one in `panupgrade auth` and accept the new one; otherwise stop and ask your network team")


def path():
    return site.home_dir() / "trusted_certs.json"


def pretty(sha256):
    """AB:CD:... the way Panorama and browsers show a fingerprint, so it can be compared by eye."""
    sha256 = (sha256 or "").upper()
    return ":".join(sha256[i:i + 2] for i in range(0, len(sha256), 2)) or "(none)"


def _key(address):
    return address.strip().lower()


def read():
    """{address: {'sha256': hex, 'accepted': date, 'reason': text}}. A missing or unreadable file is no trust at all."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("sha256"), str)} if isinstance(data, dict) else {}


def pinned(address):
    """The accepted SHA-256 (lower-case hex) for this address, or None."""
    entry = read().get(_key(address))
    return entry["sha256"].lower() if entry else None


def record(address):
    return read().get(_key(address))


def pin(address, sha256, reason=""):
    """Trust exactly this certificate for this address from now on."""
    entries = read()
    entries[_key(address)] = {"sha256": sha256.lower().replace(":", ""), "accepted": date.today().isoformat(), "reason": reason}
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")


def forget(address):
    """Stop trusting what was accepted for this address. True when there was something to forget."""
    entries = read()
    if entries.pop(_key(address), None) is None:
        return False
    path().write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
    return True
