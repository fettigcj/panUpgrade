"""The live Backend: carries out the executor's steps on real firewalls, through Panorama.

Every step is one allow-listed command (see ``WriteApi.WRITE_ALLOWED``) plus polling with a read-only connection;
nothing here can send anything else. Waiting uses injected ``sleep`` and ``clock`` so the logic is tested offline.

UNVERIFIED until the first attended run is recorded (marked where they matter): the exact shape of the job reply to
download/install, how long a reboot looks "down" through Panorama's proxy, and whether ``sync-to-remote`` answers
before or after the sync finishes (the code does not assume either: it polls the passive's config-sync state).
"""

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from panupgrade.executor import Backend, BackendError
from panupgrade.sources import SourceError
from panupgrade import xmlops


def _version_xml(verb, version):
    return f"<request><system><software><{verb}><version>{version}</version></{verb}></software></system></request>"


HA_STATE = {"suspend": "<request><high-availability><state><suspend/></state></high-availability></request>",
            "functional": "<request><high-availability><state><functional/></state></high-availability></request>"}
SYNC = "<request><high-availability><sync-to-remote><running-config/></sync-to-remote></high-availability></request>"
RESTART = "<request><restart><system/></restart></request>"
SOFTWARE_INFO = "<request><system><software><info/></software></system></request>"


# update family -> (XML root of its request commands, text of its read-only "check" command, key of the installed version)
CONTENT_FAMILIES = {"content": ("content", "request content upgrade check", "app"),
                    "antivirus": ("anti-virus", "request anti-virus upgrade check", "av"),
                    "wildfire": ("wildfire", "request wildfire upgrade check", "wildfire")}


def latest_version(check_reply):
    """The newest version in a 'request ... upgrade check' reply. The list carries no 'latest' flag on the firewalls seen so far
    (PAN-OS 10.2, 2026-10-29), so this is the highest "major-minor" number, the way Upgrade Assurance finds it; an entry that is
    flagged latest=yes, if some release does flag one, wins."""
    flagged, ranked = None, []
    for entry in check_reply.iter("entry"):
        version = (entry.findtext("version") or "").strip()
        m = re.fullmatch(r"(\d+)-(\d+)", version)
        if not m:
            continue
        ranked.append(((int(m.group(1)), int(m.group(2))), version))
        if (entry.findtext("latest") or "").strip() == "yes":
            flagged = version
    return flagged or (max(ranked)[1] if ranked else None)


def uptime_seconds(text):
    """'0 days, 12:35:49' -> seconds; None if the text is not in that shape."""
    m = re.fullmatch(r"\s*(\d+) days?, (\d+):(\d+):(\d+)\s*", text or "")
    if not m:
        return None
    d, h, mi, s = (int(x) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


class LiveBackend(Backend):
    def __init__(self, read_api, write_api, settings, backup_dir="backups", sleep=time.sleep, clock=time.monotonic):
        self.read, self.write, self.settings = read_api, write_api, settings
        self.backup_dir, self._sleep, self._clock = Path(backup_dir), sleep, clock

    # ---- plumbing

    def _op(self, serial):
        return lambda command: self.read.op(command, target=serial)

    def _info(self, serial):
        try:
            node = self.read.op("show system info", target=serial).find("result/system")
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        if node is None:
            raise BackendError(f"{serial}: no system info in the reply")
        return node

    def _send(self, xml, serial):
        try:
            root = self.write.write_xml(xml, serial)
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        if root.get("status") != "success":
            lines = " ".join(" ".join((e.text or "").split()) for e in root.iter("line"))
            raise BackendError(f"{serial}: device refused the command: {lines or root.get('status')}")
        return root

    def _job_id(self, root):
        found = root.findtext(".//job")
        if found and found.strip().isdigit():
            return found.strip()
        for line in root.iter("line"):                # UNVERIFIED shape: fall back to the text "... jobid 12"
            m = re.search(r"job ?id\D*(\d+)", line.text or "", re.I)
            if m:
                return m.group(1)
        raise BackendError(f"no job id in the reply: {ET.tostring(root, encoding='unicode')[:200]}")

    def _wait_job(self, serial, job, timeout_s, label):
        deadline = self._clock() + timeout_s
        while True:
            try:
                reply = self.read.op_xml(f"<show><jobs><id>{job}</id></jobs></show>", target=serial)
            except SourceError as exc:
                raise BackendError(f"{serial}: lost contact while waiting for {label}: {exc}") from None
            node = reply.find(".//job")
            if node is not None and (node.findtext("status") or "") == "FIN":
                if (node.findtext("result") or "") != "OK":
                    detail = " ".join(" ".join((l.text or "").split()) for l in node.iter("line"))
                    raise BackendError(f"{serial}: {label} job {job} ended {node.findtext('result')}: {detail}")
                return
            if self._clock() > deadline:
                raise BackendError(f"{serial}: {label} job {job} did not finish within {timeout_s} s")
            self._sleep(self.settings.poll_interval_s)

    def _wait_until(self, serial, test, timeout_s, what):
        deadline = self._clock() + timeout_s
        while True:
            if test():
                return
            if self._clock() > deadline:
                raise BackendError(f"{serial}: {what} did not happen within {timeout_s} s")
            self._sleep(self.settings.poll_interval_s)

    # ---- Backend interface

    def preflight(self, serial):
        facts, problems = xmlops.collect_facts(self._op(serial))
        if facts.sw_version is None:
            raise BackendError(f"{serial}: cannot read the version: {'; '.join(problems)}")
        return {"version": facts.sw_version, "pending_changes": facts.pending_changes, "running_jobs": facts.running_jobs}

    def state(self, serial):
        facts, problems = xmlops.collect_facts(self._op(serial))
        if facts.sw_version is None:
            raise BackendError(f"{serial}: cannot read state: {'; '.join(problems)}")
        return {"version": facts.sw_version, "ha_state": facts.ha_state, "running_jobs": facts.running_jobs,
                "ha_config_sync": facts.ha_config_sync, "ha_session_sync": facts.ha_session_sync,
                "session_count": facts.session_count, "content_compat": facts.ha_content_compat}

    def backup(self, serial):
        try:
            reply = self.read.op_xml("<show><config><running/></config></show>", target=serial)
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        config = reply.find("result/config")
        if config is None or config.find("devices") is None:
            return {"sha256": "", "bytes": 0, "parsed_ok": False}
        data = ET.tostring(config, encoding="utf-8")
        ET.fromstring(data)                          # parse it back: a backup we cannot read is not a backup
        info = self._info(serial)
        name = f"{(info.findtext('hostname') or serial)}_{serial}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.xml"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        path = self.backup_dir / name
        path.write_bytes(data)
        if path.stat().st_size != len(data):
            raise BackendError(f"{serial}: backup file {path} is not the size that was written")
        return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "parsed_ok": True, "path": str(path)}

    def download(self, serial, version):
        # refresh the offered list, then skip if the image is already on the box
        try:
            self.read.op_xml("<request><system><software><check/></software></system></request>", target=serial)
            info = self.read.op_xml(SOFTWARE_INFO, target=serial)
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        for entry in info.iter("entry"):
            if entry.findtext("version") == version:
                if entry.findtext("downloaded") == "yes":
                    return
                break
        else:
            raise BackendError(f"{serial}: {version} is not offered by the update server (it may not exist, or support is missing)")
        job = self._job_id(self._send(_version_xml("download", version), serial))
        self._wait_job(serial, job, self.settings.download_timeout_s, f"download {version}")

    def install(self, serial, version):
        job = self._job_id(self._send(_version_xml("install", version), serial))
        self._wait_job(serial, job, self.settings.install_timeout_s, f"install {version}")

    def reboot_and_wait(self, serial, timeout_s):
        before = uptime_seconds(self._info(serial).findtext("uptime"))
        if before is None:
            raise BackendError(f"{serial}: cannot read uptime, so a reboot could not be told apart from no reboot")
        self._send(RESTART, serial)
        seen = {"version": None}

        def back_up():
            try:
                info = self._info(serial)
            except BackendError:
                return False                        # still down (or Panorama has no connection to it yet)
            up = uptime_seconds(info.findtext("uptime"))
            if up is None or up >= before:
                return False                        # answered, but it is still the old boot
            seen["version"] = info.findtext("sw-version")
            return True

        self._wait_until(serial, back_up, timeout_s, "the firewall coming back from its reboot")
        self._wait_until(serial, lambda: self.state(serial)["running_jobs"] == 0, 900, "boot-time jobs (auto-commit) finishing")
        return seen["version"]

    def content_versions(self, serial):
        info = self._info(serial)
        return {key: ((info.findtext(tag) or "").strip() or None) for key, tag in xmlops.CONTENT_TAGS.items()}

    def refresh_updates(self, serial, families):
        """'Check now' for each family on this firewall: asks the update server what is available. Changes nothing installed."""
        for family in families:
            try:
                self.read.op(CONTENT_FAMILIES[family][1], target=serial)
            except SourceError as exc:
                raise BackendError(str(exc)) from None
        return {"refreshed": list(families)}

    def update_content(self, serial, family):
        """Install the latest content of one family unless it is already the installed one. UNVERIFIED: the shape of the
        'check' reply (an entry flagged latest=yes with a version); anything else stops with the raw text."""
        root, check, key = CONTENT_FAMILIES[family]
        try:
            reply = self.read.op(check, target=serial)
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        latest = latest_version(reply)
        if latest is None:
            raise BackendError(f"{serial}: no {family} update is offered (licence, or no route to the update server?): "
                               f"{ET.tostring(reply, encoding='unicode')[:200]}")
        if self.content_versions(serial).get(key) == latest:
            return {"already_current": latest}
        job = self._job_id(self._send(f"<request><{root}><upgrade><download><latest/></download></upgrade></{root}></request>", serial))
        self._wait_job(serial, job, self.settings.download_timeout_s, f"{family} download")
        job = self._job_id(self._send(f"<request><{root}><upgrade><install><version>latest</version></install></upgrade></{root}></request>", serial))
        self._wait_job(serial, job, self.settings.install_timeout_s, f"{family} install")
        return {"installed": latest}

    def sync_to_peer(self, serial):
        self._send(SYNC, serial)
        self._wait_until(serial, lambda: self.state(serial)["ha_config_sync"] is True, self.settings.sync_timeout_s,
                         "the HA config sync reading synchronized")

    def suspend(self, serial):
        self._send(HA_STATE["suspend"], serial)
        self._wait_until(serial, lambda: self.state(serial)["ha_state"] == "suspended", 120, "the member reading suspended")

    def functional(self, serial):
        self._send(HA_STATE["functional"], serial)
        self._wait_until(serial, lambda: self.state(serial)["ha_state"] in ("active", "passive", "initial", "active-primary", "active-secondary"),
                         300, "the member leaving the suspended state")
