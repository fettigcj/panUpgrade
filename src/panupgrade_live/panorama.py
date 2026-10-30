# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Read-only access to Panorama's XML API, with per-firewall ``op`` proxied through Panorama (``target=<serial>``).

The API key is read from a file whose path the caller supplies; its content is never printed or logged.
The server's TLS certificate is verified. A lab Panorama with a self-signed certificate needs an explicit opt-out
(``--insecure-tls`` or ``PANUPGRADE_INSECURE_TLS=1``), which prints a warning.
"""

import ast
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from panupgrade import site
from panupgrade.model import DeviceRef
from panupgrade.sources import SourceError
from panupgrade.xmlops import OpSource, READ_ONLY_PREFIXES


def to_xml_cmd(command):
    """'show jobs all' -> '<show><jobs><all/></jobs></show>' (plain words only; anything else is refused)."""
    words = command.split()
    if not words or not all(re.fullmatch(r"[a-z0-9-]+", w) for w in words):
        raise ValueError(f"not a plain operational command: {command!r}")
    xml = f"<{words[-1]}/>"
    for word in reversed(words[:-1]):
        xml = f"<{word}>{xml}</{word}>"
    return xml


class RateLimiter:
    """At most ``per_second`` requests a second across every thread that shares it (Panorama has no official limit; about five
    a second is the commonly cited polite ceiling). Each caller reserves the next free slot, then sleeps until it arrives."""

    def __init__(self, per_second=5.0, clock=time.monotonic, sleep=time.sleep):
        self.interval, self._clock, self._sleep = 1.0 / per_second, clock, sleep
        self._next, self._lock = 0.0, threading.Lock()

    def wait(self):
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)


_WARNED = []


def _tls(verify_tls=None):
    """A TLS context that checks the server's certificate, unless the operator opted out (PANUPGRADE_INSECURE_TLS or --insecure-tls)."""
    if verify_tls is None:
        verify_tls = site.verify_tls()
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        if not _WARNED:
            _WARNED.append(True)
            print("warning: TLS certificate checking is OFF. Credentials go to a server whose identity is not verified; "
                  "use this only for a lab Panorama with a self-signed certificate.", file=sys.stderr)
    return ctx


def load_accounts(path):
    """The credentials file: a Python-style dict {'account': 'password', ...}. Only the names are ever shown."""
    if not Path(path).is_file():
        raise SourceError(f"there is no credentials file at {path}. Create one holding {{'account': 'password'}}, keep it in your own "
                          f"home folder, and point to it with --creds-file or PANUPGRADE_CREDS") from None
    try:
        data = ast.literal_eval(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError, SyntaxError) as exc:
        raise SourceError(f"cannot read credentials file {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise SourceError(f"{path} must hold a dict of account name to password")
    return data


def key_from_account(address, creds_file, account, verify_tls=None, timeout=60):
    """Log in with a named account from the credentials file and return the API key. The password goes in a POST body."""
    accounts = load_accounts(creds_file)
    if account not in accounts:
        raise SourceError(f"account {account!r} is not in {creds_file} (it has: {', '.join(sorted(accounts))})")
    body = urllib.parse.urlencode({"type": "keygen", "user": account, "password": accounts[account]}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{address}/api/", data=body),
                                    context=_tls(verify_tls), timeout=timeout) as response:
            root = ET.fromstring(response.read())
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{address}: login as {account} was refused (HTTP {exc.code})") from None
    except OSError as exc:
        raise SourceError(f"{address}: cannot reach the API to log in: {exc}") from None
    key = root.findtext(".//key")
    if root.get("status") != "success" or not key:
        raise SourceError(f"{address}: login as {account} failed")
    return key


class PanoramaApi:
    def __init__(self, address, key_file=None, verify_tls=None, timeout=60, recorder=None, key=None, limiter=None):
        self.address, self.timeout, self._recorder, self._limiter = address, timeout, recorder, limiter
        if key is None:
            lines = Path(key_file).read_text(encoding="utf-8").splitlines()
            key = lines[0].strip() if lines else ""
            if not key:
                raise SourceError(f"{key_file} is empty")
        self._key = key
        self._ctx = _tls(verify_tls)

    def op(self, command, target=None):
        if not command.startswith(READ_ONLY_PREFIXES):
            raise PermissionError(f"refusing non-read-only command: {command!r}")
        return self._post(to_xml_cmd(command), target, command)

    def op_xml(self, xml, target=None):
        """A read-only command given as XML (for the ones that carry a value, like ``show jobs id 12``)."""
        if not READ_ONLY_XML.fullmatch(xml):
            raise PermissionError(f"refusing non-read-only XML command: {xml[:80]!r}")
        try:
            ET.fromstring(xml)       # exactly one element: a second command cannot ride along after the first
        except ET.ParseError:
            raise PermissionError(f"refusing XML that is not a single command: {xml[:80]!r}") from None
        return self._post(xml, target, xml)

    def _post(self, xml, target, label):
        command = label
        params = {"type": "op", "cmd": xml}
        if target:
            params["target"] = target
        if self._limiter:
            self._limiter.wait()
        request = urllib.request.Request(f"https://{self.address}/api/", data=urllib.parse.urlencode(params).encode(),
                                         headers={"X-PAN-KEY": self._key})
        try:
            with urllib.request.urlopen(request, context=self._ctx, timeout=self.timeout) as response:
                raw = response.read()
        except OSError as exc:
            raise SourceError(f"{self.address}{' via target ' + target if target else ''}: {exc}") from None
        if self._recorder:
            self._recorder(target or "panorama", command, raw)
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SourceError(f"{command}: unparseable response ({exc})") from None


# Read-only commands that need a value or are not "show"/"check": nothing else is accepted by op_xml.
READ_ONLY_XML = re.compile(
    r"<(?:show|check)>[A-Za-z0-9<>/ .\-]*|<request><system><software><(?:check|info)/></software></system></request>")

# The ONLY commands a write-capable connection may send. Each is spelled out; there is no way to send anything else.
WRITE_ALLOWED = [
    re.compile(r"<request><system><software><download><version>[0-9]+\.[0-9]+\.[0-9]+(?:-h[0-9]+)?</version></download></software></system></request>"),
    re.compile(r"<request><system><software><install><version>[0-9]+\.[0-9]+\.[0-9]+(?:-h[0-9]+)?</version></install></software></system></request>"),
    re.compile(r"<request><restart><system/></restart></request>"),
    # dynamic content (CDSS): download the latest, install the latest - for content, antivirus and WildFire only
    re.compile(r"<request><(content|anti-virus|wildfire)><upgrade><download><latest/></download></upgrade></\1></request>"),
    re.compile(r"<request><(content|anti-virus|wildfire)><upgrade><install><version>latest</version></install></upgrade></\1></request>"),
    re.compile(r"<request><high-availability><state><(?:suspend|functional)/></state></high-availability></request>"),
    re.compile(r"<request><high-availability><sync-to-remote><running-config/></sync-to-remote></high-availability></request>"),
]


class WriteApi(PanoramaApi):
    """A PanoramaApi that may also send the handful of commands in WRITE_ALLOWED. Everything else is refused."""

    def write_xml(self, xml, target):
        if not target:
            raise PermissionError("a write command must name the firewall it is for; none is ever sent to Panorama itself")
        if not any(p.fullmatch(xml) for p in WRITE_ALLOWED):
            raise PermissionError(f"refusing a command that is not on the write allow-list: {xml[:80]!r}")
        return self._post(xml, target, xml)


def write_api(address, creds_file, account, verify_tls=None, recorder=None, limiter=None):
    return WriteApi(address, verify_tls=verify_tls, recorder=recorder, limiter=limiter,
                    key=key_from_account(address, creds_file, account, verify_tls))


def _text(node, path):
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def live_source(address, key_file=None, verify_tls=None, recorder=None, creds_file=None, account=None, limiter=None):
    """An OpSource over Panorama: devices from ``show devices all``, facts through the proxy.

    Log in with ``creds_file`` + ``account`` (a named account, e.g. the read-only prep account) or give ``key_file``.
    """
    if creds_file:
        api = PanoramaApi(address, verify_tls=verify_tls, recorder=recorder, limiter=limiter or RateLimiter(),
                          key=key_from_account(address, creds_file, account, verify_tls))
    else:
        api = PanoramaApi(address, key_file, verify_tls, recorder=recorder, limiter=limiter or RateLimiter())
    info = api.op("show system info")
    pano_version = _text(info, "result/system/sw-version") or None
    try:
        from panupgrade.xmlops import parse_clock_text
        pano_clock = parse_clock_text(api.op("show clock").findtext("result"))
    except SourceError:
        pano_clock = None
    devices = api.op("show devices all")
    refs = []
    for entry in devices.iter("entry"):
        if entry.find("serial") is None:
            continue
        refs.append(DeviceRef(
            serial=_text(entry, "serial"), hostname=_text(entry, "hostname"), model=_text(entry, "model"),
            sw_version=_text(entry, "sw-version"), connected=_text(entry, "connected") == "yes",
            device_group=_text(entry, "device-group"), ha_state=_text(entry, "ha/state"),
            peer_serial=_text(entry, "ha/peer/serial")))
    return OpSource(pano_version, refs, lambda serial: (lambda command: api.op(command, target=serial)), pano_clock)
