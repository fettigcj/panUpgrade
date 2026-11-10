# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

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

    def running_jobs(self, serial):
        """One query (`show jobs all`) instead of the full state read: the look right before a step must be fresh."""
        try:
            return xmlops.parse_running_jobs(self._op(serial))
        except (xmlops.XmlOpError, ET.ParseError, SourceError) as exc:
            raise BackendError(f"{serial}: cannot read the job list: {exc}") from None

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

    def delete_image(self, serial, version):
        """Remove one downloaded image. Refused for the image that is running or flagged current; checked afterwards, never assumed."""
        def listed():
            try:
                info = self.read.op_xml(SOFTWARE_INFO, target=serial)
            except SourceError as exc:
                raise BackendError(str(exc)) from None
            return next((e for e in info.iter("entry") if e.findtext("version") == version), None)
        running = (self._info(serial).findtext("sw-version") or "").strip()
        entry = listed()
        if entry is None or entry.findtext("downloaded") != "yes":
            return {"already_absent": version}
        if version == running or entry.findtext("current") == "yes":
            raise BackendError(f"{serial}: {version} is the image that is running; it is never deleted")
        self._send(f"<delete><software><version>{version}</version></software></delete>", serial)

        def gone():
            again = listed()
            return again is None or again.findtext("downloaded") != "yes"
        self._wait_until(serial, gone, 120, f"image {version} to be gone from the firewall")
        return {"deleted": version}

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

    # ---- the asymmetry window (windows.py). Reply shapes, Panorama-proxied config edit and commit VERIFIED on a lab firewall 2026-11-07.

    def tcp_asymmetry(self, serial):
        """{"reject_non_syn": bool in force, "reject_non_syn_config": "yes" | "no" | None, "asymmetric_path": "drop" | "bypass" | None}.
        None is "not set" (the factory default). Refuses (BackendError) to guess: what cannot be read cannot be restored."""
        try:
            info = self.read.op_xml("<show><session><info/></session></show>", target=serial)
            config = self.read.op_xml("<show><config><running/></config></show>", target=serial)
        except SourceError as exc:
            raise BackendError(f"{serial}: cannot read the TCP settings: {exc}") from None
        flag = next((e for e in info.iter() if e.tag.lower() == "tcp-nonsyn-rej"), None)      # VERIFIED on a lab firewall: `show session info` calls it tcp-nonsyn-rej (True / False)
        word = ((flag.text or "").strip().lower() if flag is not None else "")
        if word not in ("true", "false", "yes", "no"):
            raise BackendError(f"{serial}: cannot read tcp-reject-non-syn from `show session info` (got {word!r}); nothing was changed")
        setting = "result/config/devices/entry/deviceconfig/setting/"
        values = {}
        for key, path, allowed in (("asymmetric_path", "tcp/asymmetric-path", ("bypass", "drop")),
                                   ("reject_non_syn_config", "session/tcp-reject-non-syn", ("yes", "no"))):
            node = config.find(setting + path)
            value = (node.text or "").strip().lower() if node is not None else None
            if value not in (None, *allowed):
                raise BackendError(f"{serial}: {path} reads {value!r}, which this tool does not know how to put back; nothing was changed")
            values[key] = value
        return {"reject_non_syn": word in ("true", "yes"), **values}

    def set_tcp_asymmetry(self, serial, reject_non_syn, reject_non_syn_config, asymmetric_path):
        """Both configuration nodes (None deletes the node: back to "not set"), one commit on the firewall, and only then the in-force flag,
        because an operational `set session tcp-reject-non-syn` does not survive a commit."""
        try:
            for knob, value in (("tcp-reject-non-syn", reject_non_syn_config), ("asymmetric-path", asymmetric_path)):
                self.write.write_config(knob, "delete" if value is None else "set", value, serial)
            job = self._job_id(self.write.commit_local(serial))
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        self._wait_job(serial, job, self.settings.install_timeout_s, "the commit of the TCP setting change")
        self._send(f"<set><session><tcp-reject-non-syn>{'yes' if reject_non_syn else 'no'}</tcp-reject-non-syn></session></set>", serial)

    def lb_profile(self, serial, profile, protocols):
        """An interface management profile as the firewall has it now: {"exists": bool, "values": {protocol: "yes" | "no" | None}, "origin": "local" | "template" | None}. Reply shape verified on a lab firewall."""
        found = self._profile_entry(serial, profile, "running")
        origin = "local"
        if found is None:
            found = self._profile_entry(serial, profile, "merged")      # not in the local XML: a Panorama template may hold it
            origin = "template"
        values = {}
        for protocol in protocols:
            node = found.find(protocol) if found is not None else None
            value = (node.text or "").strip().lower() if node is not None else None
            if value not in (None, "yes", "no"):
                raise BackendError(f"{serial}: {protocol} in the profile {profile!r} reads {value!r}, which this tool does not know how to put back; nothing was changed")
            values[protocol] = value
        return {"exists": found is not None, "values": values, "origin": origin if found is not None else None}

    def _profile_entry(self, serial, profile, view):
        """The <entry> of the named interface management profile in the firewall's running (local XML only) or merged (local + template) view, or None."""
        try:
            config = self.read.op_xml(f"<show><config><{view}/></config></show>", target=serial)
        except SourceError as exc:
            raise BackendError(f"{serial}: cannot read the interface management profile: {exc}") from None
        return next((e for e in config.iterfind("result/config/devices/entry/network/profiles/interface-management-profile/entry")
                     if e.get("name") == profile), None)

    def set_lb_profile(self, serial, profile, changes):
        """Edit the named profile (one commit on the firewall). None removes the protocol's node: back to "not set". A profile only a Panorama template
        holds is first copied to a same-named local one with the changes applied (that local copy takes precedence); remove_lb_override deletes it again."""
        try:
            if self._profile_entry(serial, profile, "running") is None:
                entry = self._profile_entry(serial, profile, "merged")
                if entry is None:
                    raise BackendError(f"{serial}: the interface management profile {profile!r} is on neither the firewall nor a template")
                for protocol, value in changes.items():
                    for old in entry.findall(protocol):
                        entry.remove(old)
                    if value is not None:
                        ET.SubElement(entry, protocol).text = value
                self.write.write_profile_override(profile, ET.tostring(entry, encoding="unicode"), serial)
            else:
                for protocol, value in changes.items():
                    self.write.write_profile(profile, protocol, "delete" if value is None else "set", value, serial)
            job = self._job_id(self.write.commit_local(serial))
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        self._wait_job(serial, job, self.settings.install_timeout_s, "the commit of the interface management profile change")

    def remove_lb_override(self, serial, profile):
        """Delete the local copy of a template's profile (one commit on the firewall): the template's profile resumes, which is the state before the drain."""
        try:
            self.write.write_profile_remove(profile, serial)
            job = self._job_id(self.write.commit_local(serial))
        except SourceError as exc:
            raise BackendError(str(exc)) from None
        self._wait_job(serial, job, self.settings.install_timeout_s, "the commit that removes the local copy of the interface management profile")
