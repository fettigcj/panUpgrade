"""Builders for invented fleets. Names, serials and versions are synthetic:
feature releases 20.x-22.x do not exist, so nothing here can be mistaken for real data."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panupgrade.sources import FakeFleet  # noqa: E402

TARGET = "20.2.9"

TEST_RULES = {  # TEST DATA ONLY, not vendor rules
    "verified": True,
    "direct_from": {"20.2": ["20.1"], "21.0": ["20.2"], "21.1": ["21.0", "20.2"]},
    "stepping_install": {},
}


def facts(**over):
    base = {"sw_version": "20.2.3", "ha_mode": "none", "ha_state": None, "peer_serial": None,
            "preemption": None, "ha_session_sync": None, "ha_config_sync": None, "ha_links_ok": None,
            "disk_free_mb": 9000, "images_downloaded": [], "pending_changes": False, "running_jobs": 0,
            "session_count": 1000, "ntp_synced": True, "licenses_ok": True}
    base.update(over)
    return base


def ha_facts(state, peer, **over):
    base = dict(ha_mode="active-passive", ha_state=state, peer_serial=peer, preemption=False,
                ha_session_sync=True, ha_config_sync=True, ha_links_ok=True,
                session_count=1000 if state == "active" else 12)
    base.update(over)
    return facts(**base)


def device(serial, hostname=None, version="20.2.3", model="SIM-A", **over):
    entry = {"serial": serial, "hostname": hostname or serial.lower(), "model": model, "sw_version": version,
             "connected": True, "device_group": "dg-sim", "tags": [], "facts": facts(sw_version=version)}
    entry.update(over)
    return entry


def pair(a, b, version="20.2.3", preemption=False, **over):
    da = device(a, version=version, facts=ha_facts("active", b, sw_version=version, **over))
    db = device(b, version=version, facts=ha_facts("passive", a, sw_version=version, **over))
    da["peer_serial"], db["peer_serial"] = b, a
    for d in (da, db):
        d["facts"]["preemption"] = preemption
    return [da, db]


def fleet_data(*devices, panorama="22.1.0"):
    flat = []
    for d in devices:
        flat.extend(d if isinstance(d, list) else [d])
    return {"panorama": {"version": panorama}, "devices": flat}


def fleet(*devices, panorama="22.1.0"):
    return FakeFleet(fleet_data(*devices, panorama=panorama))


def demo_fleet_data():
    devs = []
    devs += pair("SIMA0001", "SIMA0002")
    devs += pair("SIMB0001", "SIMB0002", preemption=True)
    devs.append(device("SIMC0001", version="20.2.1"))
    devs.append(device("SIMD0001", connected=False, facts={}))
    devs.append(device("SIME0001", collect_error="API call timed out", facts={}))
    devs.append(device("SIMF0001", version="20.2.9"))
    g = pair("SIMG0001", "SIMG0002")
    g[1]["facts"]["pending_changes"] = True
    devs += g
    devs.append(device("SIMH0001", version="20.1.4"))
    return {"panorama": {"version": "22.1.0"}, "devices": devs}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(demo_fleet_data(), indent=1) + "\n", encoding="utf-8")
