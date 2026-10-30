# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""An in-memory fleet that behaves like paired and standalone firewalls, for rehearsing an execution.

Nothing here is a model of real PAN-OS timing or output; it exists so the executor's
decisions (ordering, stop-after-N, failover bands, resume) can be tested with failures
injected on demand. Behaviours that depend on real devices (does a suspended state survive a
reboot, what a job looks like mid-install) are assumptions, marked UNVERIFIED in docs.
"""

from .executor import Backend, BackendError


class Node:
    def __init__(self, serial, version, state, peer, sessions, preemption, pending=False, jobs=0):
        self.serial, self.version, self.state, self.peer = serial, version, state, peer
        self.sessions, self.preemption, self.pending, self.jobs = sessions, preemption, pending, jobs
        self.downloaded, self.staged = set(), None
        self.backed_up = False
        self.config_synced = True
        self.content = {}
        self.compat = {}
        self.preferred = state == "active"   # preemption hands the active role back to the original active


class SimBackend(Backend):
    def __init__(self, fleet_data, fail=None, takeover_ratio=1.0, wrong_version=None):
        """fail: {(serial, operation): message} - raised once when that operation runs.
        takeover_ratio: the new active node holds this share of the old active node's sessions.
        wrong_version: {serial: version} - what the node reports after its reboot, whatever was installed."""
        self.nodes = {}
        for entry in fleet_data["devices"]:
            f = entry.get("facts") or {}
            self.nodes[entry["serial"]] = Node(
                entry["serial"], entry["sw_version"], f.get("ha_state") if f.get("ha_mode") != "none" else None,
                f.get("peer_serial"), f.get("session_count") or 0, bool(f.get("preemption")),
                bool(f.get("pending_changes")), f.get("running_jobs") or 0)
            self.nodes[entry["serial"]].config_synced = f.get("ha_config_sync") is not False
            self.nodes[entry["serial"]].downloaded = set(f.get("images_downloaded") or [])
            self.nodes[entry["serial"]].content = dict(f.get("content_versions") or {})
            self.nodes[entry["serial"]].compat = dict(f.get("ha_content_compat") or {})
        self.fail = dict(fail or {})
        self.takeover_ratio = takeover_ratio
        self.wrong_version = dict(wrong_version or {})
        self.calls = []

    def _enter(self, serial, operation, *args):
        self.calls.append((serial, operation) + args)
        message = self.fail.pop((serial, operation), None)
        if message is not None:
            raise BackendError(message)
        return self.nodes[serial]

    def preflight(self, serial):
        n = self._enter(serial, "preflight")
        return {"version": n.version, "pending_changes": n.pending, "running_jobs": n.jobs}

    def backup(self, serial):
        n = self._enter(serial, "backup")
        n.backed_up = True
        return {"sha256": f"sim-{serial}", "bytes": 1000, "parsed_ok": True}

    def download(self, serial, version):
        self._enter(serial, "download", version).downloaded.add(version)

    def install(self, serial, version):
        n = self._enter(serial, "install", version)
        if version not in n.downloaded:
            raise BackendError(f"image {version} is not downloaded")
        n.staged = version

    def reboot_and_wait(self, serial, timeout_s):
        n = self._enter(serial, "reboot")
        n.version = self.wrong_version.get(serial, n.staged or n.version)
        n.staged = None
        return n.version

    def state(self, serial):
        n = self._enter(serial, "state")
        return {"version": n.version, "ha_state": n.state, "running_jobs": n.jobs,
                "ha_config_sync": (n.config_synced if n.peer else None), "ha_session_sync": True if n.peer else None,
                "session_count": n.sessions, "content_compat": dict(n.compat) or None}

    def content_versions(self, serial):
        return dict(self._enter(serial, "content versions").content)

    LATEST = {"content": {"app": "9999-9999", "threat": "9999-9999"}, "antivirus": {"av": "av-9999"}, "wildfire": {"wildfire": "wf-9999"}}

    def refresh_updates(self, serial, families):
        self._enter(serial, "refresh updates", tuple(families))
        return {"refreshed": list(families)}

    def update_content(self, serial, family):
        n = self._enter(serial, "update content", family)
        n.content.update(self.LATEST[family])
        for node in (n, self.nodes.get(n.peer)):          # installing content makes PAN-OS re-evaluate the pair's compatibility
            if node is not None and node.compat:
                node.compat.update({k: "Match" for k in ("app", "threat", "av", "iot")})
        return {"installed": self.LATEST[family]}

    def sync_to_peer(self, serial):
        n = self._enter(serial, "sync to peer")
        for node in (n, self.nodes.get(n.peer)):
            if node is not None:
                node.config_synced = True

    def suspend(self, serial):
        n = self._enter(serial, "suspend")
        was_active = n.state == "active"
        n.state = "suspended"
        peer = self.nodes.get(n.peer)
        if was_active and peer is not None and peer.state in ("passive", "active"):
            peer.sessions, peer.state = int(n.sessions * self.takeover_ratio), "active"

    def functional(self, serial):
        n = self._enter(serial, "functional")
        peer = self.nodes.get(n.peer)
        if peer is not None and peer.state == "active" and n.preemption and n.preferred:
            n.state, n.sessions = "active", peer.sessions
            peer.state, peer.sessions = "passive", 12
        else:
            if peer is not None and peer.state == "active":
                n.state, n.sessions = "passive", 12
            else:
                n.state = "active"
