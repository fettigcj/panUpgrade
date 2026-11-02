# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Read-only access to Panorama's XML API, with per-firewall ``op`` proxied through Panorama (``target=<serial>``).

The API key is read from a file whose path the caller supplies; its content is never printed or logged.
The server's TLS certificate is verified. A lab Panorama with a self-signed certificate needs an explicit opt-out
(``--insecure-tls`` or ``PANUPGRADE_INSECURE_TLS=1``), which prints a warning.
"""

import functools
import hashlib
import http.client
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from panupgrade import credstore, progress, site, trust
from panupgrade.trust import CertificateChanged, CertificateProblem
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


def _host_port(address):
    host, _, port = address.rpartition(":")
    return (host, int(port)) if host and port.isdigit() and ":" not in host else (address, 443)


def _peer_sha256(address, timeout=10):
    """The SHA-256 of the certificate the address presents, fetched without trusting it (only to show it to a person, who compares it
    with what Panorama says). None when it cannot be read at all."""
    host, port = _host_port(address)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    except OSError:
        return None
    return hashlib.sha256(der).hexdigest() if der else None


class _PinnedConnection(http.client.HTTPSConnection):
    """A connection that accepts one certificate, identified by fingerprint, instead of asking a CA about it."""

    def __init__(self, *args, pin, address, **kwargs):
        super().__init__(*args, **kwargs)
        self._pin, self._address = pin, address

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        seen = hashlib.sha256(der).hexdigest() if der else ""
        if seen != self._pin:
            self.close()
            raise CertificateChanged(self._address, self._pin, seen)


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, address, pin):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # the fingerprint is the check
        super().__init__(context=ctx)
        self._address, self._pin = address, pin

    def https_open(self, req):
        return self.do_open(functools.partial(_PinnedConnection, pin=self._pin, address=self._address), req, context=self._context)


def _urlopen(address, request, verify_tls=None, timeout=60):
    """Open a request to Panorama. The certificate is checked the normal way, unless a person accepted a certificate for this address:
    then it must be exactly that one. A certificate that fails the check becomes a CertificateProblem the caller can offer to accept."""
    if verify_tls is None:
        verify_tls = site.verify_tls()
    pin = trust.pinned(address) if verify_tls else None
    try:
        if pin:
            return urllib.request.build_opener(_PinnedHandler(address, pin)).open(request, timeout=timeout)
        return urllib.request.urlopen(request, context=_tls(verify_tls), timeout=timeout)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise CertificateProblem(address, getattr(exc.reason, "verify_message", None) or str(exc.reason), _peer_sha256(address)) from None
        raise


def load_accounts(path):
    """The accounts in the credentials file (see ``panupgrade.credstore``). Only the names are ever shown."""
    return credstore.read(path)


def stored_kind(creds_file, account):
    """'API key' or 'password': how this account signs in. Never the secret itself."""
    return credstore.kind(load_accounts(creds_file).get(account))


def key_from_login(address, user, password, verify_tls=None, timeout=60):
    """Log in as ``user`` and return the API key. The password goes in a POST body, never in a URL."""
    body = urllib.parse.urlencode({"type": "keygen", "user": user, "password": password}).encode()
    try:
        with _urlopen(address, urllib.request.Request(f"https://{address}/api/", data=body), verify_tls, timeout) as response:
            root = ET.fromstring(response.read())
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{address}: login as {user} was refused (HTTP {exc.code})") from None
    except OSError as exc:
        raise SourceError(f"{address}: cannot reach the API to log in: {exc}") from None
    key = root.findtext(".//key")
    if root.get("status") != "success" or not key:
        raise SourceError(f"{address}: login as {user} failed")
    return key


def key_from_account(address, creds_file, account, verify_tls=None, timeout=60):
    """The API key for a named account in the credentials file: the stored one, or one obtained by logging in with its password."""
    accounts = load_accounts(creds_file)
    if account not in accounts:
        raise credstore.NeedsSignIn(f"account {account!r} is not in {creds_file} (it has: {', '.join(sorted(accounts)) or 'no accounts'})")
    secret = accounts[account]
    return secret["api_key"].strip() if isinstance(secret, dict) else key_from_login(address, account, secret, verify_tls, timeout)


class PanoramaApi:
    def __init__(self, address, key, verify_tls=None, timeout=60, recorder=None, limiter=None):
        self.address, self.timeout, self._recorder, self._limiter = address, timeout, recorder, limiter
        self._key = key
        self._verify = verify_tls
        _tls(verify_tls)                                   # says once, loudly, if checking is off

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
        progress.say(f"asking {'firewall ' + target if target else 'Panorama'}: {' '.join(re.sub(r'<[^>]*>', ' ', label).split())}")
        params = {"type": "op", "cmd": xml}
        if target:
            params["target"] = target
        if self._limiter:
            self._limiter.wait()
        request = urllib.request.Request(f"https://{self.address}/api/", data=urllib.parse.urlencode(params).encode(),
                                         headers={"X-PAN-KEY": self._key})
        try:
            with _urlopen(self.address, request, self._verify, self.timeout) as response:
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
    # remove one downloaded PAN-OS image (prepare, when delete_unneeded_images is on; the backend never names the running one)
    re.compile(r"<delete><software><version>[0-9]+\.[0-9]+\.[0-9]+(?:-h[0-9]+)?</version></software></delete>"),
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


def write_api(address, creds_file=None, account=None, verify_tls=None, recorder=None, limiter=None, key=None):
    """The API that may also send the allow-listed write commands, signed in with ``key`` or with a named account."""
    return WriteApi(address, verify_tls=verify_tls, recorder=recorder, limiter=limiter,
                    key=key or key_from_account(address, creds_file, account, verify_tls))


def _text(node, path):
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def device_group_names(reply):
    """{serial: device group name} from ``show devicegroups``. ``show devices all`` does not say which group a firewall is in."""
    found = {}
    for group in reply.findall("./result/devicegroups/entry"):
        for member in group.findall("devices/entry"):
            found[member.get("name")] = group.get("name")
    return found


def device_tags(entry):
    """The tags on one ``show devices all`` entry (empty when none are set)."""
    return [(m.text or "").strip() for m in entry.findall("tag/member") if (m.text or "").strip()]


def live_source(address, verify_tls=None, recorder=None, creds_file=None, account=None, limiter=None, key=None):
    """An OpSource over Panorama: devices from ``show devices all``, facts through the proxy.

    Sign in with ``key``, or with ``creds_file`` + ``account`` (a named account in the credentials file).
    """
    key = key or key_from_account(address, creds_file, account, verify_tls)
    api = PanoramaApi(address, key, verify_tls, recorder=recorder, limiter=limiter or RateLimiter())
    info = api.op("show system info")
    pano_version = _text(info, "result/system/sw-version") or None
    try:
        from panupgrade.xmlops import parse_clock_text
        pano_clock = parse_clock_text(api.op("show clock").findtext("result"))
    except SourceError:
        pano_clock = None
    devices = api.op("show devices all")
    try:
        groups = device_group_names(api.op("show devicegroups"))
    except SourceError:
        groups = {}                                   # an account that may not look at device groups just sees none
    refs = []
    for entry in devices.iter("entry"):
        if entry.find("serial") is None:
            continue
        refs.append(DeviceRef(
            serial=_text(entry, "serial"), hostname=_text(entry, "hostname"), model=_text(entry, "model"),
            sw_version=_text(entry, "sw-version"), connected=_text(entry, "connected") == "yes",
            device_group=groups.get(_text(entry, "serial"), ""), tags=device_tags(entry), ha_state=_text(entry, "ha/state"),
            peer_serial=_text(entry, "ha/peer/serial")))
    return OpSource(pano_version, refs, lambda serial: (lambda command: api.op(command, target=serial)), pano_clock)
