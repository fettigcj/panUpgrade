# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The upgrade kit: what ``prepare`` leaves behind so the upgrade window is one command.

Three files, written side by side once the preparation is done. There is no second prompt: the kit is the offer, and
RUNNING the script is the person's go-ahead (they take the report to whoever must agree, then come back and run it):

* the **serial list** of the firewalls the approval covers (a plain text file, comments allowed);
* the **approval** (plan digest, those firewalls, the acknowledged cautions, who and when);
* a **run script** (.bat on Windows, .sh elsewhere) containing the exact ``panupgrade upgrade`` command.

The script holds no secrets, only the path of the credentials file. It stays valid only while the plan it was approved for
is unchanged (HA roles, versions, planned steps, cautions) and for ``approval_max_age_hours``; otherwise ``upgrade`` refuses
and says to run ``prepare`` again. Running it twice needs ``--resume``: the journal file is new for each kit.
"""

import json
import os
import shlex
from pathlib import Path

from .site import DEFAULT_CREDS, verify_tls


def _quote(value, windows):
    value = str(value)
    if windows:
        return f'"{value}"' if any(c in value for c in ' &()^%') or not value else value
    return shlex.quote(value)


def upgrade_argv(args, serials_file, approval_file, journal_file):
    """The upgrade command as a list, rebuilt from how ``prepare`` was invoked."""
    where = ["--panorama", args.source[len("panorama:"):]] if args.source.startswith("panorama:") else ["--source", args.source]
    argv = ["python", "-m", "panupgrade", "upgrade", *where, "--serials-file", str(serials_file),
            "--target", args.target, "--approval", str(approval_file), "--journal", str(journal_file)]
    for flag, value in (("--settings", args.settings), ("--creds-file", args.creds_file), ("--key-file", args.key_file),
                        ("--path-rules", args.path_rules)):
        if value:
            argv += [flag, str(Path(value).resolve())]
    if not args.creds_file and not args.key_file:      # the script must not depend on the environment variables it is later run under
        argv += ["--creds-file", str(Path(DEFAULT_CREDS).resolve())]
    argv += ["--account", args.account, "--work-account", args.work_account]
    if not verify_tls():
        argv.append("--insecure-tls")
    for item in args.set or []:
        argv += ["--set", item]
    return argv


def write(out_dir, stamp, args, plan, approval, who, windows=None, project_dir=None):
    """Write the three files. Returns {'serials': Path, 'approval': Path, 'script': Path, 'journal': Path, 'command': str}."""
    windows = (os.name == "nt") if windows is None else windows
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    project = Path(project_dir or Path(__file__).resolve().parents[2]).resolve()
    base = f"upgrade-{args.target}-{stamp}"
    serials_file, approval_file = out / f"{base}.serials.txt", out / f"{base}.approval.json"
    journal_file = out / f"{base}.journal.jsonl"
    script = out / (f"run-{base}" + (".bat" if windows else ".sh"))

    names = {d["serial"]: d for d in plan["devices"]}
    lines = [f"# firewalls covered by {approval_file.name} (target {args.target}); one serial per line"]
    lines += [f"{s}   # {names[s]['hostname']}  {names[s]['current_version']}  {names[s].get('ha_state') or 'standalone'}"
              for s in approval.serials]
    serials_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    approval_file.write_text(json.dumps(approval.to_dict(), indent=1) + "\n", encoding="utf-8")

    argv = upgrade_argv(args, serials_file, approval_file, journal_file)
    command = " ".join(_quote(a, windows) for a in argv)
    header = [f"panupgrade upgrade kit for {args.target}, written {stamp} by {who}. Running this script IS the go-ahead.",
              f"This runs the REAL upgrade on {len(approval.serials)} firewall(s) listed in {serials_file.name}.",
              f"It is approved only while the plan is unchanged and for a limited time (see approval_max_age_hours); if it refuses, run `panupgrade prepare` again.",
              f"Interrupted? Add --resume to the command to continue; finished steps are skipped."]
    if windows:
        body = ["@echo off"] + [f"rem {h}" for h in header] + [f'cd /d "{project}"', f'set "PYTHONPATH={project / "src"}"', command, "pause"]
        script.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
    else:
        body = ["#!/usr/bin/env sh"] + [f"# {h}" for h in header] + [f"cd {_quote(project, False)}",
                                                                      f"export PYTHONPATH={_quote(project / 'src', False)}", command]
        script.write_text("\n".join(body) + "\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | 0o111)
    return {"serials": serials_file, "approval": approval_file, "script": script, "journal": journal_file, "command": command}
