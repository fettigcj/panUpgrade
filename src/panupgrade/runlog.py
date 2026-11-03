# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The readable record of a run, built from its journal: every event in order, and the same events firewall by firewall.

The journal (``*.jsonl``) stays the machine record. This is what a person opens afterwards to see what happened: an HTML page with an
"All activity" tab and one tab per firewall hostname, and a plain-text copy with the same sections.
"""

from datetime import datetime, timezone
from html import escape

from . import console

KINDS = {"preparation": "PreparationLog", "upgrade": "UpgradeLog"}
NO_TARGET = "NoTarget"            # a preparation (config sync, content) can be run without a PAN-OS target
HEADINGS = {"preparation": "Preparation log", "upgrade": "Upgrade log"}
_CHIP = {"ok": "pass", "sync": "info", "start": "info", "wait": "skip", "info": "skip", "fail": "fail"}
_STATE_CHIP = {"done": "pass", "failed": "fail", "needs_attention": "warn", "stopped": "warn", "held": "warn", "in doubt": "warn"}


def file_name(kind, target, stamp, extension):
    """PanUpgrade_PreparationLog_10.2.10-h42_20261103-101500.html"""
    return f"PanUpgrade_{kind}_{target}_{stamp}.{extension}"


def _local(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts or "")


def _derived_state(rows):
    """What the events alone say about one firewall, for a run with no recorded outcome (it was interrupted)."""
    if any(kind == "fail" for _, kind, _ in rows):
        return "failed"
    return "in doubt" if rows and rows[-1][1] in ("start", "wait", "sync") else "done"


def build(run, plan, hosts, events, label, journal_path, result=None):
    """Everything the log shows, as plain data. ``run`` is "preparation" or "upgrade"; ``plan`` is that run's plan (its target, which a
    preparation may not have, and hash); ``hosts`` maps serial to hostname; ``result`` is the run's outcome dict, or None when the run was
    interrupted before it had one."""
    names = {**hosts}
    by_serial, everything = {}, []
    for record in events:
        described = console.describe_event(record, names)
        if described is None:
            continue
        kind, text = described
        serial = record.get("serial")
        row = (_local(record.get("ts")), kind, text)
        by_serial.setdefault(serial, []).append(row)
        everything.append((row[0], hosts.get(serial, serial or "(run)"), kind, text))
    outcome = {}
    if result is not None:
        outcome = {serial: (state, detail or "") for serial, (state, detail) in result["outcome"].items()}
    tabs = []
    for serial in sorted(by_serial, key=lambda s: (hosts.get(s, s or "")).lower()):
        state = outcome[serial][0] if serial in outcome else _derived_state(by_serial[serial])
        tabs.append({"host": hosts.get(serial, serial or "(run)"), "serial": serial or "", "state": state, "rows": by_serial[serial]})
    if result is None:
        verdict = "The run did not finish: it was stopped or ended on an error. Steps marked in doubt were started and not seen to finish."
    else:
        done = sum(1 for state, _ in outcome.values() if state == "done")
        verdict = f"{done} of {len(outcome)} firewalls finished all their steps, {result['failures']} failed."
        if result.get("stopped"):
            verdict += " Stopped early: the failure limit was reached, so nothing further was started."
    return {"run": run, "heading": HEADINGS[run], "target": plan.get("target") or NO_TARGET, "label": label, "plan_hash": plan["hash"][:12],
            "journal": str(journal_path), "generated": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "verdict": verdict, "failed": result is None or bool(result["failures"]),
            "outcome": [{"host": hosts.get(s, s), "serial": s, "state": st, "detail": d} for s, (st, d) in outcome.items()],
            "all": everything, "tabs": tabs}


def to_text(data):
    out = [f"panUpgrade {data['heading'].lower()}: target {data['target']}  |  {data['label']}  |  plan {data['plan_hash']}",
           f"journal {data['journal']}  |  written {data['generated']}", "", data["verdict"], ""]
    if data["outcome"]:
        out.append("Result")
        out += [f"  {o['host']} ({o['serial']}): {o['state']}" + (f" - {o['detail']}" if o["detail"] else "") for o in data["outcome"]]
        out.append("")
    width = max([len(t["host"]) for t in data["tabs"]] + [8])
    out.append("== All activity ==")
    out += [f"{when}  {host.ljust(width)}  [{kind}] {text}" for when, host, kind, text in data["all"]]
    for tab in data["tabs"]:
        out += ["", f"== {tab['host']} ({tab['serial']}): {tab['state']} =="]
        out += [f"{when}  [{kind}] {text}" for when, kind, text in tab["rows"]]
    return "\n".join(out) + "\n"


_CSS = """:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#6b7686;--line:#e3e7ed;--accent:#2f6fed;
--fail:#d64545;--warn:#e0a020;--pass:#2e9e5b;--skip:#98a2b3;--info:#3b8fc4}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1a2029;--ink:#e6eaf0;--mute:#8d99ab;--line:#2a323e;--accent:#6c9bff;
--fail:#ef6b6b;--warn:#e8b64a;--pass:#4cc17f;--skip:#6f7a8c;--info:#5eb0e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1400px;margin:0 auto;padding:18px 20px 60px}h1{font-size:22px;margin:4px 0 14px}
h2{font-size:13px;margin:24px 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto;margin-bottom:12px}
.bad{border-color:var(--fail)}.good{border-color:var(--pass)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;color:#fff;background:var(--skip)}
.chip.fail{background:var(--fail)}.chip.warn{background:var(--warn)}.chip.pass{background:var(--pass)}.chip.info{background:var(--info)}
.mute{color:var(--mute)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.tabs{display:none;flex-wrap:wrap;gap:6px;margin:14px 0 10px}.js .tabs{display:flex}
.tabs button{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}
.tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);font-weight:600}
.js .panel{display:none}.js .panel.on{display:block}"""

_SCRIPT = """document.documentElement.classList.add('js');
var tabs=[].slice.call(document.querySelectorAll('.tabs button')),panels=[].slice.call(document.querySelectorAll('.panel'));
function show(id){tabs.forEach(function(b){b.setAttribute('aria-selected',b.dataset.panel===id)});
panels.forEach(function(p){p.classList.toggle('on',p.id===id)});try{history.replaceState(null,'','#'+id)}catch(e){}}
tabs.forEach(function(b){b.addEventListener('click',function(){show(b.dataset.panel)})});
show(panels.some(function(p){return '#'+p.id===location.hash})?location.hash.slice(1):'all');"""


def _chip(kind, text=None):
    return f'<span class="chip {_CHIP.get(kind, "")}">{escape(text or kind)}</span>'


def _state_chip(state):
    return f'<span class="chip {_STATE_CHIP.get(state, "")}">{escape(state.replace("_", " "))}</span>'


def to_html(data):
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>panUpgrade {escape(data['heading'].lower())}</title><style>{_CSS}</style></head><body><main>",
           f"<h1>{escape(data['heading'])}: target {escape(data['target'])}</h1>",
           f'<div class="card {"bad" if data["failed"] else "good"}">{escape(data["verdict"])}<br>'
           f'<span class="mute">{escape(data["label"])} - plan <span class="mono">{escape(data["plan_hash"])}</span> - journal '
           f'<span class="mono">{escape(data["journal"])}</span> - written {escape(data["generated"])}</span></div>']
    if data["outcome"]:
        out.append('<div class="card"><table><tr><th>Firewall</th><th>Result</th><th>Detail</th></tr>')
        out += [f'<tr><td>{escape(o["host"])}<br><span class="mono mute">{escape(o["serial"])}</span></td><td>{_state_chip(o["state"])}</td>'
                f'<td>{escape(o["detail"])}</td></tr>' for o in data["outcome"]]
        out.append("</table></div>")
    out.append('<div class="tabs" role="tablist"><button role="tab" data-panel="all">All activity</button>')
    for number, tab in enumerate(data["tabs"], 1):
        out.append(f'<button role="tab" data-panel="fw{number}">{escape(tab["host"])} {_state_chip(tab["state"])}</button>')
    out.append("</div>")
    out.append('<section class="panel card" id="all"><h2>All activity</h2><table><tr><th>Time</th><th>Firewall</th><th>Event</th></tr>')
    out += [f'<tr><td class="mono">{escape(when)}</td><td>{escape(host)}</td><td>{_chip(kind)} {escape(text)}</td></tr>'
            for when, host, kind, text in data["all"]]
    out.append("</table></section>")
    for number, tab in enumerate(data["tabs"], 1):
        out.append(f'<section class="panel card" id="fw{number}"><h2>{escape(tab["host"])} <span class="mono">{escape(tab["serial"])}</span> '
                   f'{_state_chip(tab["state"])}</h2><table><tr><th>Time</th><th>Event</th></tr>')
        out += [f'<tr><td class="mono">{escape(when)}</td><td>{_chip(kind)} {escape(text)}</td></tr>' for when, kind, text in tab["rows"]]
        out.append("</table></section>")
    out.append(f"</main><script>{_SCRIPT}</script></body></html>")
    return "".join(out)
