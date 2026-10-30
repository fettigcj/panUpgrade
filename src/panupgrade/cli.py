# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Command line: ``select`` and ``plan`` (read-only), ``prepare`` and ``upgrade`` (real with a live source, a rehearsal with --dry-run or a fake source), ``settings``."""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

from . import (approval as approval_mod, fleetreport, console, executor, paths, prepare as prepare_mod, report,
               settings as settings_mod, xlsxout)
from .site import DEFAULT_ACCOUNT, DEFAULT_CREDS, DEFAULT_WORK_ACCOUNT
from .journal import Journal, JournalError
from .planner import PlanError, Selection, build_plan
from .simulator import SimBackend
from .sources import FakeFleet, SourceError, snapshot




_SAID = set()


def _tilde(path):
    """A path as the person would type it: under their home folder it starts with ~."""
    full = Path(path).expanduser()
    try:
        return os.path.join("~", str(full.resolve().relative_to(Path.home().resolve())))
    except ValueError:
        return str(full)


def _env_origin(value, *env_names):
    """Where a value came from: the environment variable that holds it, or a flag."""
    for name in env_names:
        if os.environ.get(name) and os.environ[name] == value:
            return f"from {name}"
    return "set with a flag"


_KEYS = {}


def _login(args):
    """Settle, once, who reads and who changes. The site decides how it signs in; this only follows what it said.

    reading / changes are account names from the credentials file, or None where a login is typed at the prompt.
    With no account named: a credentials file holding exactly one account means that one; changes use the reading account.
    """
    if getattr(args, "_login", None):
        return args._login
    ask = getattr(args, "ask_credentials", None)
    reading = changes = how = None
    if ask != "all" and not args.key_file:
        reading = args.account
        if reading:
            how = _env_origin(reading, "PANUPGRADE_ACCOUNT", "PANUPGRADE_READ_ACCOUNT")
        else:
            from panupgrade_live.panorama import load_accounts
            path = args.creds_file or DEFAULT_CREDS
            names = sorted(load_accounts(path))
            if len(names) != 1:
                raise SourceError(f"{path} holds {len(names)} accounts ({', '.join(names)}). Say which one to use with --account NAME "
                                  f"(or PANUPGRADE_ACCOUNT), or use --ask-credentials to type a login instead")
            reading, how = names[0], "the only account in that file"
    if ask not in ("changes", "all"):
        changes = getattr(args, "work_account", None) or reading
    args._login = {"reading": reading, "changes": changes, "ask": ask, "how": how}
    return args._login


def _typed(args, address):
    """A username and password typed at the prompt: used for one login, never stored or printed."""
    if address not in _KEYS:
        if not sys.stdin.isatty():
            raise SourceError("--ask-credentials needs a terminal to type into")
        user = input("Panorama username: ").strip()
        password = getpass.getpass("Password (not shown, not stored): ")
        from panupgrade_live.panorama import key_from_login
        _KEYS[address] = (user, key_from_login(address, user, password))
        print(_style(args).line("info", f"Signed in as {user} (typed at the prompt, not stored)"))
    return _KEYS[address][1]


def _key_for(args, address, role):
    """The API key for ``role`` ('reading' or 'changes'), by whichever route this site set up."""
    login = _login(args)
    if login["ask"] == "all" or (role == "changes" and login["ask"] == "changes"):
        return _typed(args, address)
    account = login[role]
    if not account:
        raise SourceError("changes need an account: name one with --work-account (or --account), or use --ask-credentials changes")
    if (address, account) not in _KEYS:
        from panupgrade_live.panorama import key_from_account
        _KEYS[(address, account)] = (account, key_from_account(address, args.creds_file or DEFAULT_CREDS, account))
    return _KEYS[(address, account)][1]


def _announce_connection(args, writing=False):
    """Say, once per run, what the tool is about to connect to and as whom. Never a password or key, only whose they are."""
    style = _style(args)
    if writing and ("writing", args.source) not in _SAID:
        _SAID.add(("writing", args.source))
        _announce_connection(args)
        login = _login(args)
        if login["ask"] == "changes":
            text = "Changes will be made with a login you type at the prompt"
        elif login["ask"] == "all":
            text = "Changes will be made with the same login you type at the prompt"
        elif login["changes"] == login["reading"]:
            text = f"Changes will be made as: {login['changes']} (the same account)"
        else:
            text = f"Changes will be made as: {login['changes']} ({_env_origin(login['changes'], 'PANUPGRADE_WORK_ACCOUNT')})"
        print(style.line("warn", text))
        return
    key = ("connection", args.source)
    if key in _SAID:
        return
    _SAID.add(key)
    if args.source.startswith("fake:"):
        print(style.line("info", f"Using the invented fleet in {_tilde(args.source[5:])}: a simulation, no real firewall is contacted"))
        return
    from .site import verify_tls
    address = args.source[len("panorama:"):]
    print(style.line("info", f"Panorama: {address}, " + ("certificate checked" if verify_tls() else "certificate NOT checked (lab use only)")))
    if args.key_file:
        print(style.line("info", f"Signing in with the API key in {_tilde(args.key_file)} (--key-file)"))
        return
    login = _login(args)
    if login["ask"] == "all":
        print(style.line("info", "You will be asked for a username and password (used for reading and for changes; not stored)"))
        return
    creds = args.creds_file or DEFAULT_CREDS
    origin = "set with --creds-file" if args.creds_file else ("from PANUPGRADE_CREDS" if os.environ.get("PANUPGRADE_CREDS") else "default location")
    print(style.line("info", f"Credentials file: {_tilde(creds)} ({origin})"))
    print(style.line("info", f"Reading as: {login['reading']} ({login['how']})"))


def _load_settings(args):
    """Load the settings and say, once, where they came from."""
    settings = settings_mod.load(args.settings, args.set)
    if ("settings", args.settings) not in _SAID:
        _SAID.add(("settings", args.settings))
        style = _style(args)
        origin = f"settings file {_tilde(args.settings)}" if args.settings else "built-in default settings (no settings file given)"
        extra = f", with {len(args.set)} changed on the command line" if args.set else ""
        print(style.line("info", f"Using {origin}{extra}"))
    return settings


def _source(args):
    spec, key_file, record = args.source, args.key_file, args.record
    _announce_connection(args, writing=getattr(args, "_writing", False))
    if spec.startswith("fake:"):
        return FakeFleet.from_file(spec[len("fake:"):])
    if spec.startswith("panorama:"):
        from panupgrade_live.panorama import live_source   # read-only; imported only when asked for
        recorder = None
        if record:
            Path(record).mkdir(parents=True, exist_ok=True)

            def recorder(target, command, raw):
                (Path(record) / f"{target}__{re.sub('[^A-Za-z0-9_.-]+', '_', command)[:90]}.xml").write_bytes(raw)
        address = spec[len("panorama:"):]
        if key_file:
            return live_source(address, key_file, recorder=recorder)
        return live_source(address, recorder=recorder, key=_key_for(args, address, "reading"))
    raise SystemExit("sources: fake:<file> or panorama:<address> (read-only)")


def _serials(args):
    serials = list(args.serial or [])
    if args.serials_file:
        text = Path(args.serials_file).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()      # 'SERIAL   # a note' is fine
            if line:
                serials.append(line.split()[0])
    return serials


def _selection(args):
    return Selection(serials=_serials(args), name_glob=args.name or "", name_contains=args.name_contains or "",
                     models=args.model or [], device_groups=args.device_group or [], tags=args.tag or [],
                     version_is=args.version_is or "", version_older_than=args.version_older_than or "",
                     version_newer_than=args.version_newer_than or "", ha_states=args.ha_state or [],
                     accept_standalone_outage=getattr(args, "accept_standalone_outage", False))


def _style(args):
    return console.Style.detect(getattr(args, "color", "auto"), getattr(args, "ascii", False))


def _build(args, source=None):
    settings = _load_settings(args)
    rules = paths.load_rules(args.path_rules) if args.path_rules else None
    source = source or _source(args)
    return build_plan(source, args.target, settings, _selection(args), rules), settings


def _plan(args):
    style = _style(args)
    try:
        plan, _ = _build(args)
    except (settings_mod.SettingsError, PlanError, SourceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "plan.json").write_text(report.to_json(plan), encoding="utf-8")
        (out / "plan.txt").write_text(report.to_text(plan), encoding="utf-8")
        (out / "plan.html").write_text(report.to_html(plan), encoding="utf-8")
        template = {"plan_hash": plan["hash"], "serials": [], "acks": [], "approved_by": "", "note": "",
                    "cautions_available": [f"{c['scope']}:{c['code']}" for c in plan["cautions"]]}
        (out / "approval.template.json").write_text(json.dumps(template, indent=1) + "\n", encoding="utf-8")
        print(f"Wrote plan.json, plan.txt, plan.html and approval.template.json to {out}")
    else:
        verbose = getattr(args, "verbose", False)
        sys.stdout.write(console.render_plan(plan, style, verbose) if style.color else report.to_text(plan, verbose))
    if args.xlsx:
        xlsxout.write_workbook(args.xlsx, plan)
        print(f"Wrote {args.xlsx}")
    return 0


_FILTERS = ("serial", "serials_file", "name", "name_contains", "model", "device_group", "tag",
            "version_is", "version_older_than", "version_newer_than", "ha_state")


def _wants_form(args):
    """The form opens when asked for (-i), and by itself when `select` is typed at a terminal with nothing to filter by."""
    if args.interactive is False:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        if args.interactive:
            raise SourceError("--interactive needs a terminal; give the filters as flags instead (see `panupgrade help select`)")
        return False
    return bool(args.interactive) or not (args.out or any(getattr(args, f, None) for f in _FILTERS))


def _where(args):
    return args.source[len("panorama:"):] if args.source.startswith("panorama:") else _tilde(args.source[len("fake:"):])


def _command_line(args, flags):
    """A command the user can paste to repeat the selection; values with anything unusual in them are double-quoted."""
    def word(text):
        return text if re.fullmatch(r"[A-Za-z0-9._:/\\~=+-]+", text) else '"' + text.replace('"', '\\"') + '"'
    where = ("--panorama", args.source[len("panorama:"):]) if args.source.startswith("panorama:") else ("--source", args.source)
    return "panupgrade select " + " ".join(f"{flag} {word(value)}" for flag, value in [where] + flags)


def _select(args):
    """Build the list of devices from criteria, show it, and save it for the user to check and edit."""
    style = _style(args)
    try:
        use_form = _wants_form(args)
        refs = _source(args).list_devices()
    except (SourceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selection = _selection(args)
    if use_form:
        from . import selectform
        form = selectform.Form(refs, selection, args.out or "")
        title = f"panupgrade select - {len(refs)} firewalls from {_where(args)}"
        if not selectform.show(form, title):
            print("Cancelled; nothing was selected.")
            return 1
        selection, args.out = form.selection(selection), form.values["out"].strip() or None
        print(style.line("info", "To get this same list without the form: " + _command_line(args, form.flags())))
    chosen = [r for r in refs if selection.matches(r)]
    rows = [[i, r.hostname, r.serial, r.model, r.sw_version, r.ha_state or "-", r.device_group or "-",
             "yes" if r.connected else "NO"] for i, r in enumerate(chosen, 1)]
    print(style.bold(f"Criteria: {selection.describe()}"))
    sys.stdout.write(console.render_table(["#", "hostname", "serial", "model", "PAN-OS", "HA", "device group", "online"], rows, style))
    print(style.line("info", f"{len(chosen)} of {len(refs)} devices match. Check the list; edit the file if needed; "
                             f"then pass it with --serials-file."))
    if args.out:
        lines = [f"# panupgrade device list - criteria: {selection.describe()}",
                 "# One serial per line; text after # is ignored. Delete a line to drop a device; the file is the list that is used."]
        lines += [f"{r.serial}   # {r.hostname}  {r.sw_version}  {r.ha_state or '-'}" for r in chosen]
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


def _report(args):
    """Read the fleet and say what state it is in. No target version, no plan, nothing changes."""
    style = _style(args)
    try:
        live = _source(args)
        refs = live.list_devices()
        chosen = [r for r in refs if _selection(args).matches(r)]
        include = {r.serial for r in chosen} | {r.peer_serial for r in chosen if r.peer_serial}
        snap = snapshot(live, include)
        snap["devices"] = [d for d in snap["devices"] if d["serial"] in include]
    except (SourceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = fleetreport.build(snap)
    sys.stdout.write(fleetreport.render(report, style, args.source.split(":", 1)[-1] if args.source.startswith("panorama:") else ""))
    if args.out:
        Path(args.out).write_text(json.dumps({"panorama_version": report["panorama_version"], "versions": report["versions"],
                                              "attention": report["attention"], "devices": report["devices"]}, indent=1) + chr(10), encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


def _expected_steps(plan):
    """For each firewall the plan will change: every journal key a finished run would hold."""
    return {d["serial"]: ["preflight"] + [f"{i}:{s['action']}" for i, s in enumerate(d["steps"])]
            for d in plan["devices"] if d["verdict"] in ("ready", "caution")}


def _unfinished_run(runs_dir, verb, plan):
    """The newest earlier run of exactly this plan that did not finish, as (Journal, path, summary), or None."""
    for path in sorted(Path(runs_dir).glob(f"{verb}-*.jsonl"), reverse=True):
        if "-dry-" in path.name:
            continue
        try:
            journal = Journal.resume(str(path), plan["hash"])
        except (JournalError, OSError, ValueError, KeyError):
            continue                                   # a different plan, or not a journal
        done = failed = doubt = missing = 0
        for serial, keys in _expected_steps(plan).items():
            for key in keys:
                state = journal.last_state(serial, key)
                done += state == "ok"
                failed += state == "failed"
                doubt += state == "started"
                missing += state is None
        if verb == "upgrade" and not (failed or doubt or (done and missing)):
            continue                                   # finished (or never started): nothing to continue
        return journal, path, f"{done} steps done, {failed} failed, {doubt} started but not finished, {missing} not reached"
    return None


def _choose_journal(args, plan, verb, style, input_fn=None, tty=None):
    """Decide which journal file this run uses, without creating it. Returns (path, resume). The person never has to name one."""
    if args.journal:
        named = Path(args.journal)
        if args.resume or not named.exists():
            return args.journal, bool(args.resume)
        if not _has_steps(named):                           # a refused or interrupted-at-the-start run left only its header: nothing was done
            return args.journal, True
        print(style.line("warn", f"{named} already holds a run that started (its finished steps are skipped if you continue it)"))
        if not (sys.stdin.isatty() if tty is None else tty):
            raise ValueError("Add --resume to continue it (finished steps are skipped), or use a different --journal name (or none) for a new run")
        try:
            answer = (input_fn or input)("Type CONTINUE to carry on with it, or NEW for a fresh run (anything else cancels): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "CONTINUE":
            return args.journal, True
        if answer != "NEW":
            raise ValueError("cancelled")
        path, n = named, 1
        while path.exists():
            n += 1
            path = named.with_name(f"{named.stem}-{n}{named.suffix}")
        return str(path), False
    runs = Path(args.runs_dir)
    previous = _unfinished_run(runs, verb, plan) if (args.resume or verb == "upgrade") and not args.dry_run else None
    if args.resume:
        if previous is None:
            raise ValueError(f"--resume: no unfinished earlier run of this exact plan was found in {runs}")
        return str(previous[1]), True
    if previous is not None and not args.fresh:
        print(style.line("warn", f"an earlier run of this exact plan did not finish: {previous[1]} ({previous[2]})"))
        if not (sys.stdin.isatty() if tty is None else tty):
            raise ValueError("add --resume to continue that run (finished steps are skipped, an in-doubt step is reported, never repeated) "
                             "or --fresh to start a new one")
        try:
            answer = (input_fn or input)("Type CONTINUE to carry on with it, or NEW for a fresh run (anything else cancels): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "CONTINUE":
            return str(previous[1]), True
        if answer != "NEW":
            raise ValueError("cancelled")
    runs.mkdir(parents=True, exist_ok=True)
    stem = f"{verb}{'-dry' if args.dry_run else ''}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    path, n = runs / f"{stem}.jsonl", 1
    while path.exists():                      # two runs in the same second still never share a file
        n += 1
        path = runs / f"{stem}-{n}.jsonl"
    return str(path), False


def _print_cautions(style, plan, heading, only=None):
    """The cautions an approval accepts, grouped under each firewall, one per bullet, instead of one long line apiece."""
    names = {d["serial"]: f"{d['hostname']} ({d['serial']})" for d in plan["devices"]}
    shown = [c for c in plan["cautions"] if only is None or f"{c['scope']}:{c['code']}" in only]
    if not shown:
        return
    print()
    print(style.bold(heading))
    for scope in dict.fromkeys(c["scope"] for c in shown):
        print(f"  {style.bold('whole fleet' if scope == 'fleet' else names.get(scope, scope))}")
        for c in shown:
            if c["scope"] == scope:
                print(style.line("warn", f"[{c['code']}] {c['text']}", 4))
    print()


def _has_steps(path):
    """True if the journal file records any step (not just its header line)."""
    try:
        return any('"type": "step"' in line for line in Path(path).read_text(encoding="utf-8").splitlines())
    except OSError:
        return True


def _discard_if_empty(journal, resuming):
    """A run that was refused before doing anything leaves nothing behind that would block the next attempt."""
    if not resuming and not journal.events:
        try:
            os.remove(journal.path)
        except OSError:
            pass


def _listener(style, names, serials, settings):
    """The journal's listener: the live status board when several things run at once on a colour terminal, else one line per event."""
    if settings.max_parallel > 1 and style.color and sys.stdout.isatty():
        board = console.StatusBoard(style, names, serials)
        return board, board.close
    return console.EventPrinter(style, names), (lambda: None)


def _interactive_approval(style, plan_hash, serials, acks, lines, journal_path, ack_lines=None, input_fn=None, tty=None,
                          heading="Approval needed. You are being asked to approve exactly this:"):
    """Ask the person at the terminal. Returns an Approval, or None if there is no terminal or they decline.

    The list on screen is the thing being approved: everything shown, with every caution shown. The answer is
    recorded next to the journal so the approval is as auditable as a file handed in with --approval.
    """
    if not (sys.stdin.isatty() if tty is None else tty):
        return None
    print()
    print(style.bold(heading))
    for line in lines:
        print("  " + line)
    if acks:
        print(style.bold("Cautions that approving acknowledges:"))
        for ack in (ack_lines or acks):
            print(style.line("warn", ack, 2))
    try:
        answer = (input_fn or input)("\nType APPROVE (any case) to go ahead (anything else cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.upper() != "APPROVE":
        print(style.line("stop", "not approved; nothing was done"))
        return None
    approval = approval_mod.Approval(plan_hash, list(serials), list(acks), f"{getpass.getuser()} (typed APPROVE at the terminal)",
                                     datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                                     datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if journal_path:
        Path(str(journal_path) + ".approval.json").write_text(json.dumps(approval.to_dict(), indent=1) + "\n", encoding="utf-8")
    return approval


def _backend_and_source(args):
    """The simulator, seeded from a fleet file or (dry run only) from a read of the live fleet."""
    if args.source.startswith("fake:"):
        with open(args.source[len("fake:"):], encoding="utf-8") as handle:
            data = json.load(handle)
        label = "SIMULATION"
    else:
        live = _source(args)
        refs = live.list_devices()
        chosen = [r for r in refs if _selection(args).matches(r)]
        include = {r.serial for r in chosen} | {r.peer_serial for r in chosen if r.peer_serial}
        data = snapshot(live, include)
        label = "DRY RUN" if args.dry_run else "LIVE"
        print(f"read {len(include)} of {len(refs)} managed devices: the {len(chosen)} selected plus their HA peers; "
              f"the other {len(refs) - len(include)} were not touched")
        if label == "LIVE":
            if args.sim_fail:
                raise SystemExit("--sim-fail is for the simulator only")
            from panupgrade_live.backend import LiveBackend
            _announce_connection(args, writing=True)
            from panupgrade_live.panorama import PanoramaApi, RateLimiter, write_api
            _announce_connection(args, writing=True)
            address = args.source[len("panorama:"):]
            settings = _load_settings(args)
            limiter = RateLimiter(settings.api_rate_per_s)           # one politeness budget for every worker, reads and writes
            read_api = (PanoramaApi(address, args.key_file, limiter=limiter) if args.key_file
                        else PanoramaApi(address, key=_key_for(args, address, "reading"), limiter=limiter))
            work_api = write_api(address, limiter=limiter, key=_key_for(args, address, "changes"))
            fleet = FakeFleet(data)
            fleet.kind = "op"
            return LiveBackend(read_api, work_api, settings, settings.backup_dir), fleet, label
    fail = {}
    for item in args.sim_fail or []:
        serial, _, operation = item.partition(":")
        fail[(serial, operation)] = f"injected failure in {operation}"
    fleet = FakeFleet(data)
    fleet.kind = "fake" if label == "SIMULATION" else "op"
    return SimBackend(data, fail=fail), fleet, label


def _upgrade(args):
    style = _style(args)
    try:
        backend, fleet, label = _backend_and_source(args)
        plan, settings = _build(args, fleet)
        if args.dry_run and not args.approval:
            approval = approval_mod.Approval(plan["hash"], [d["serial"] for d in plan["devices"] if d["verdict"] in ("ready", "caution")],
                                             [f"{c['scope']}:{c['code']}" for c in plan["cautions"]], "dry run: nothing changes, so nothing was asked", "")
            print(style.line("info", "dry run: nothing changes, so no approval is asked"))
        elif args.approval:
            approval = approval_mod.Approval.from_file(args.approval)
            _print_cautions(style, plan, "Proceeding on the approval's acceptance of these cautions:", approval.acks)
        else:
            ready = [d for d in plan["devices"] if d["verdict"] in ("ready", "caution")]
            approval = _interactive_approval(
                style, plan["hash"], [d["serial"] for d in ready], [f"{c['scope']}:{c['code']}" for c in plan["cautions"]],
                [f"{d['hostname']} ({d['serial']})  {d['current_version']} -> {plan['target']}" for d in ready], args.journal,
                [f"[{c['code']}] {c['scope']}: {c['text']}" for c in plan["cautions"]])
            if approval is None:
                print("No approval given. Run `panupgrade prepare` first (it writes the approval and a run script), "
                      "or re-run this in a terminal and type APPROVE, or pass --approval FILE.")
                return 3
        journal_path, resuming = _choose_journal(args, plan, "upgrade", style)
        journal = (Journal.resume if resuming else Journal.create)(journal_path, plan["hash"])
        print(style.line("info", f"journal: {journal_path}" + (" (continuing)" if resuming else "")))
        names = {d["serial"]: d["hostname"] for d in plan["devices"]}
        journal.listener, close_view = _listener(style, names, [s for batch in plan["order"] for unit in batch for s in unit], settings)
        if label == "LIVE":
            print(style.line("stop", f"LIVE RUN - this will change real firewalls: {len(plan['order'])} batch(es), journal {journal_path}"))
        else:
            print(style.line("info", f"{label} - no write command is sent to any real device; the run is rehearsed against a model"
                             + (" seeded from live state" if label == "DRY RUN" else "")))
        try:
            result = executor.run(plan, approval, backend, journal, settings)
        except executor.ExecutionRefused:
            _discard_if_empty(journal, resuming)
            raise
        finally:
            close_view()
    except KeyboardInterrupt:
        print(style.line("stop", "stopped by you (Ctrl-C). The step in progress, if any, is marked started-but-unfinished in the "
                                 "journal: check that device, then continue with --resume. Nothing was undone."), file=sys.stderr)
        return 130
    except executor.ExecutionRefused as exc:
        print(style.line("stop", "refused:"), file=sys.stderr)
        for problem in exc.problems:
            print(problem if problem.startswith(" ") else f"  - {problem}", file=sys.stderr)
        print(style.line("info", "nothing was changed and no journal was left behind, so the same command can simply be run again once the plan is re-approved"),
              file=sys.stderr)
        return 3
    except (settings_mod.SettingsError, PlanError, SourceError, JournalError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(console.render_outcomes(result, names, style))
    return 0 if result["failures"] == 0 else 1


def _prepare(args):
    style = _style(args)
    try:
        settings = _load_settings(args)
        rules = paths.load_rules(args.path_rules) if args.path_rules else None
        backend, fleet, label = _backend_and_source(args)
        plan = prepare_mod.build(fleet, settings, _selection(args), args.target, rules)
        names = {u[k]: u[k + "_name"] for u in plan["units"] for k in ("active", "passive")}
        names.update({d["serial"]: d["hostname"] for d in plan["downloads"]})
        names.update({t["serial"]: t["hostname"] for t in plan["content_tasks"]})
        n_content = sum(1 for u in plan["units"] if u["content"])
        n_config = sum(1 for u in plan["units"] if u["sync_config"])
        print(style.bold(f"{label}: prepare plan {plan['hash'][:12]} - {n_content} content sync(s), {len(plan['content_tasks'])} minimum-content install(s), "
                         f"{n_config} config sync(s), {len(plan['downloads'])} image download(s)" + (f" for {args.target}" if args.target else "")))
        for u in plan["units"]:
            if u["content"]:
                print(style.line("start", f"{u['active_name']} + {u['passive_name']}: 'check now' on both, then install the latest "
                                          f"{' and '.join(u['content'])} update only if PAN-OS still cannot confirm "
                                          f"({u.get('content_reason') or 'versions differ'})"))
            if u["sync_config"]:
                print(style.line("sync", f"{u['active_name']} (active) -> {u['passive_name']} (passive), once the content versions match"))
        for t in plan["content_tasks"]:
            print(style.line("start", f"{t['hostname']}: install the latest content (has {t['have']}; {t['for_version']} needs {t['needed']} or newer)"))
        for d in plan["downloads"]:
            print(style.line("start", f"{d['hostname']}: download {d['version']}"))
        for c in plan["content_report"]:
            print(style.line("info", f"{c['hostname']} pair, content {c['text']}"))
        for n in plan["notes"]:
            print(style.line("warn", f"{n['hostname']}: {n['why']}"))
        for s in plan["skipped"]:
            print(style.line("skip", f"{s['hostname']}: {s['why']}"))
        result = None
        if plan["serials"]:
            result = _prepare_tasks(args, style, plan, names, backend, settings, label)
            if isinstance(result, int):
                return result
            sys.stdout.write(console.render_outcomes(result, names, style))
        else:
            print(style.line("ok", "nothing to prepare" + ("" if args.target else " (give --target to also pre-download images)")))
        if result is not None and result["failures"]:
            print()
            print(style.bold("These preparation steps failed:"))
            for serial, (state, detail) in result["outcome"].items():
                if state in ("failed", "needs_attention"):
                    print(style.bullet(f"{names.get(serial, serial)}: {detail}", 2))
            print()
            if not args.target:
                print(style.line("fail", "Fix that and run prepare again."))
                return 1
            # A failed preparation does not veto the kit: the upgrade plan below is judged on what the firewalls look like NOW.
            print(style.line("warn", "The upgrade batch developed below may not include firewalls that failed to prepare for upgrade properly."))
        if args.target:
            return _issue_kit(args, style, label)
        print(style.line("info", "give --target VERSION to also review the upgrade and get its approval and run script"))
        return 0
    except KeyboardInterrupt:
        print(style.line("stop", "stopped by you (Ctrl-C). The step in progress, if any, is marked started-but-unfinished in the "
                                 "journal: check that device, then continue with --resume. Nothing was undone."), file=sys.stderr)
        return 130
    except executor.ExecutionRefused as exc:
        print(style.line("stop", "refused:"), file=sys.stderr)
        for problem in exc.problems:
            print(problem if problem.startswith(" ") else f"  - {problem}", file=sys.stderr)
        print(style.line("info", "nothing was changed and no journal was left behind, so the same command can simply be run again once the plan is re-approved"),
              file=sys.stderr)
        return 3
    except (settings_mod.SettingsError, prepare_mod.PrepareError, SourceError, JournalError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _prepare_tasks(args, style, plan, names, backend, settings, label):
    """Approve and run the preparation tasks (config sync, image downloads). Returns the result, or an exit code."""
    if args.dry_run and not args.approval:
        approval = approval_mod.Approval(plan["hash"], list(plan["serials"]), [], "dry run: nothing changes, so nothing was asked", "")
        print(style.line("info", "dry run: nothing changes, so no approval is asked"))
    elif args.approval:
        approval = approval_mod.Approval.from_file(args.approval)
    else:
        heading = "Approval needed. You are being asked to approve exactly this:"
        if any(u["content"] for u in plan["units"]):
            heading = ("Approval needed. This includes dynamic CONTENT updates, installed on the firewalls NOW and therefore outside your "
                       "own change schedule for them (the config sync cannot happen until content is level):")
        journal_path, resuming = _choose_journal(args, plan, "prepare", style)
        approval = _interactive_approval(style, plan["hash"], plan["serials"], [], prepare_mod.describe(plan), journal_path, heading=heading)
        if approval is None:
            return 3
    if args.approval or args.dry_run:
        journal_path, resuming = _choose_journal(args, plan, "prepare", style)
    journal = (Journal.resume if resuming else Journal.create)(journal_path, plan["hash"])
    print(style.line("info", f"journal: {journal_path}" + (" (continuing)" if resuming else "")))
    journal.listener, close_view = _listener(style, names, sorted(names), settings)
    if label == "LIVE":
        print(style.line("stop", f"LIVE RUN - this will change real firewalls (config sync and image downloads); journal {journal_path}"))
    try:
        return prepare_mod.run(plan, approval, backend, journal, settings)
    except executor.ExecutionRefused:
        _discard_if_empty(journal, resuming)
        raise
    finally:
        close_view()


def _issue_kit(args, style, label):
    """After the preparation: show the upgrade plan as it now stands, ask once about its cautions, write the kit."""
    from . import kit
    if args.dry_run:      # nothing really changed, so re-reading the fleet now would show the pre-prepare state
        print(style.line("info", f"dry run: after a real prepare you would now see the upgrade to {args.target} as it then stands, "
                                 f"be asked once to acknowledge its cautions, and get the approval and run script in {args.kit_dir}"))
        return 0
    plan, settings = _build(args)
    ready = [d for d in plan["devices"] if d["verdict"] in ("ready", "caution")]
    blocked = [d for d in plan["devices"] if d["verdict"] == "blocked"]
    s = plan["summary"]
    print()
    print(style.bold(f"The upgrade to {args.target}, as it stands now: {s['ready']} ready, {s['caution']} need acknowledgement, "
                     f"{s['blocked']} blocked, {s['skipped']} not planned"))
    for d in blocked:
        why = next((r["reason"] for r in d["results"] if r["status"] in ("fail", "error")), "see `plan`")
        print(style.line("fail", f"{d['hostname']}: BLOCKED - {why}", 2))
    for note in plan.get("advice", []):
        print(style.line("info", f"{note['scope']}: {note['text']}", 2))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kit_dir = Path(args.kit_dir)
    kit_dir.mkdir(parents=True, exist_ok=True)
    stem = kit_dir / f"report-{args.target}-{stamp}"
    stem.with_suffix(".html").write_text(report.to_html(plan), encoding="utf-8")
    stem.with_suffix(".txt").write_text(report.to_text(plan), encoding="utf-8")
    xlsxout.write_workbook(stem.with_suffix(".xlsx"), plan)
    print(style.line("info", f"report for sign-off: {stem.with_suffix('.html')} (also .txt and .xlsx)"))
    if not ready:
        print(style.line("stop", "nothing is ready to upgrade, so there is nothing to approve"))
        return 1
    # No second prompt: the kit is the offer, and RUNNING the generated script is the person's go-ahead. What that go-ahead
    # covers is printed here, in the report, and again when the upgrade starts.
    who = getpass.getuser()
    approval = approval_mod.Approval(
        plan["hash"], [d["serial"] for d in ready], [f"{c['scope']}:{c['code']}" for c in plan["cautions"]],
        f"{who}, by running the generated script",
        f"prepared {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}; running the script is the approval",
        datetime.now(timezone.utc).isoformat(timespec="seconds"), plan.get("snapshot", {}))
    _print_cautions(style, plan, "Running the script accepts these cautions:")
    made = kit.write(args.kit_dir, stamp, args, plan, approval, who)
    print(style.line("ok", "the upgrade kit is ready. Written:"))
    for label_, key in (("firewall list", "serials"), ("approval", "approval"), ("run script", "script")):
        print(f"    {label_:14} {made[key]}")
    print(style.bold("\nWhen the window opens and the people who must agree have, run:"))
    print(f"    {made['script']}")
    print(style.dim(f"(valid for {settings.approval_max_age_hours} hours and only while the plan is unchanged; "
                    f"otherwise the upgrade refuses and tells you to run prepare again)"))
    return 0


def _settings(args):
    try:
        current = settings_mod.load(args.settings, args.set)
    except settings_mod.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(current), indent=2))
    for warning in current.loosening_warnings():
        print(f"caution: {warning}", file=sys.stderr)
    return 0


def _selection_args(p):
    """Connection options and the filters that choose which firewalls a command looks at (all of them by default)."""
    conn = p.add_argument_group("connect to Panorama")
    where = conn.add_mutually_exclusive_group(required=True)
    where.add_argument("--panorama", metavar="ADDRESS", help="the Panorama that manages your firewalls (name or IP address)")
    where.add_argument("--source", help="fake:<fleet.json> to try the tool on an invented fleet, with no firewall involved")
    conn.add_argument("--creds-file", help=f"credentials file, a dict of account -> password (default {DEFAULT_CREDS}, or set PANUPGRADE_CREDS); never printed")
    conn.add_argument("--account", default=DEFAULT_ACCOUNT, metavar="NAME",
                      help="the account in the credentials file to sign in with (or set PANUPGRADE_ACCOUNT). "
                           "Optional when the file holds exactly one account")
    conn.add_argument("--ask-credentials", choices=["changes", "all"],
                      help="type a username and password instead of reading them from a file: 'changes' for the steps that change "
                           "firewalls (reading still uses the file), 'all' for everything. Never stored or printed")
    conn.add_argument("--insecure-tls", action="store_true",
                      help="do not verify Panorama's TLS certificate (lab with a self-signed certificate only; or set PANUPGRADE_INSECURE_TLS=1)")
    conn.add_argument("--key-file", help="alternative to --creds-file: a file whose first line is an API key")
    conn.add_argument("--record", help="directory to save each raw device response, to build test fixtures")
    pick = p.add_argument_group("choose which firewalls (filters combine; with none, every firewall)")
    pick.add_argument("--serial", action="append", help="this serial (repeatable)")
    pick.add_argument("--serials-file", help="file with one serial per line ('#' starts a comment); the output of `select`")
    pick.add_argument("--name", help="host name glob, e.g. 'site1-*'")
    pick.add_argument("--name-contains", help="host name contains this text")
    pick.add_argument("--version-is", help="running version matches this glob, e.g. '10.2.1' or '10.2.*'")
    pick.add_argument("--version-older-than", help="running version is older than this")
    pick.add_argument("--version-newer-than", help="running version is newer than this")
    pick.add_argument("--ha-state", action="append", help="HA state as Panorama reports it: active, passive, ... (repeatable)")
    pick.add_argument("--model", action="append")
    pick.add_argument("--device-group", action="append")
    pick.add_argument("--tag", action="append")
    look = p.add_argument_group("how it looks")
    look.add_argument("--color", choices=["auto", "always", "never"], default="auto", help="ANSI colour (NO_COLOR is honoured)")
    look.add_argument("--ascii", action="store_true", help="plain [ OK ]-style tags instead of emoji")


OVERVIEW = """panupgrade: upgrade Palo Alto firewalls through Panorama, safely.

The usual order (everything but prepare and upgrade only reads):
  report    what state is the fleet in right now?                        panupgrade report --panorama <address>
  plan      what would an upgrade to a version do, and what blocks it?   panupgrade plan --panorama <address> --target 10.2.2
  prepare   get ready ahead of the window, then write the run script     panupgrade prepare --panorama <address> --target 10.2.2
  upgrade   carry out an approved upgrade (the script does this)         panupgrade upgrade --panorama <address> --approval FILE

Helpers:
  select    build a firewall list from filters, to check and edit (on a terminal it opens a fill-in form)
  settings  show every setting and its current value
  help      this page; `panupgrade help <command>` shows that command's options

Every command takes the same filters to choose firewalls (default: all of them):
  --name-contains TEXT   --name GLOB   --version-is 10.2.*   --version-older-than V   --version-newer-than V
  --ha-state active      --model M     --device-group G      --tag T      --serial S   --serials-file FILE

Signing in is your choice: a credentials file with one account, a file with a separate account for changes (--work-account),
or a login you type (--ask-credentials changes|all). Set once with PANUPGRADE_CREDS, PANUPGRADE_ACCOUNT, PANUPGRADE_WORK_ACCOUNT.
Full guide: docs/running.md
"""


def _help(args):
    if args.topic:
        return main([args.topic, "--help"])
    sys.stdout.write(OVERVIEW)
    return 0


def _plan_args(p):
    _selection_args(p)
    p.add_argument("--target", required=True, help="target PAN-OS version")
    p.add_argument("--settings", help="settings TOML file")
    p.add_argument("--set", action="append", help="override a setting: name=value")
    p.add_argument("--path-rules", help="path rules JSON (default: the packaged, unverified, empty rules)")
    p.add_argument("--accept-standalone-outage", action="store_true",
                   help="include standalone firewalls (their reboot is an outage)")


def _run_args(p, journal_required=False):
    p.add_argument("--journal", required=journal_required,
                   help="override the run file; by default a new, time-stamped one is named for you under --runs-dir and never overwritten")
    p.add_argument("--runs-dir", default="runs", help="where run journals are kept (default runs)")
    p.add_argument("--resume", action="store_true", help="continue the unfinished earlier run of this exact plan (or the file named by --journal)")
    p.add_argument("--fresh", action="store_true", help="start a new run even if an unfinished earlier run of this plan exists")
    p.add_argument("--work-account", default=DEFAULT_WORK_ACCOUNT, metavar="NAME",
                   help="a different account in the credentials file for the steps that change firewalls (or set PANUPGRADE_WORK_ACCOUNT). "
                        "Default: the same account as --account")
    p.add_argument("--dry-run", action="store_true",
                   help="with a live source: read it once and rehearse against a model of it. WITHOUT this flag a live source "
                        "means a REAL run: it changes the firewalls")
    p.add_argument("--sim-fail", action="append", help="inject a failure: serial:operation (e.g. SIMA0001:install)")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="panupgrade", description="Upgrade Palo Alto firewalls through Panorama, safely. Run `panupgrade help` for the order to use the commands in.", epilog="Run `panupgrade help` for an overview.")
    sub = parser.add_subparsers(dest="command", required=True)

    sel = sub.add_parser("select", help="build the device list from criteria, for you to check and edit; changes nothing")
    _selection_args(sel)
    sel.add_argument("--out", help="write the list here (one serial per line) for --serials-file")
    sel.add_argument("-i", "--interactive", action=argparse.BooleanOptionalAction, default=None,
                     help="fill in the filters on a form: Tab moves between fields and the matching firewalls update as you type. "
                          "Opens by itself when you run `select` at a terminal with no filters; --no-interactive turns that off")
    sel.set_defaults(func=_select)

    hp = sub.add_parser("help", help="what each command is for and the order to use them")
    hp.add_argument("topic", nargs="?", help="a command to show the options of")
    hp.set_defaults(func=_help)

    rep = sub.add_parser("report", help="show the state of the fleet right now (versions, HA, sync, content); changes nothing")
    _selection_args(rep)
    rep.add_argument("--out", help="also save the full report as JSON")
    rep.set_defaults(func=_report)

    plan = sub.add_parser("plan", help="show what would happen to each device; changes nothing")
    _plan_args(plan)
    plan.add_argument("--out", help="directory for plan.json, plan.txt, plan.html, approval.template.json")
    plan.add_argument("--xlsx", help="also write the readiness workbook (Excel: one row per firewall plus a sheet of every check)")
    plan.add_argument("--verbose", action="store_true", help="list every check, including the routine skipped ones that are normally just counted")
    plan.set_defaults(func=_plan)

    ex = sub.add_parser("upgrade", help="carry out an approved upgrade: REAL with a live source, or --dry-run to rehearse it against a model of the fleet")
    _plan_args(ex)
    _run_args(ex)
    ex.add_argument("--approval", help="approval JSON written by `prepare` (or by typing APPROVE); without it you are asked to type APPROVE. "
                                       "A --dry-run asks nothing.")
    ex.set_defaults(func=_upgrade)

    prep = sub.add_parser("prepare", help="pre-maintenance: sync HA config, pre-download images, review the upgrade, and write its approval and run script")
    _selection_args(prep)
    _run_args(prep, journal_required=False)
    prep.add_argument("--settings", help="settings TOML file (needs sync_to_peer = prepare)")
    prep.add_argument("--set", action="append", help="override a setting: name=value")
    prep.add_argument("--approval", help="approval JSON for the preparation tasks themselves; without it you are asked to type APPROVE "
                                         "(a --dry-run asks nothing)")
    prep.add_argument("--kit-dir", default="prepared",
                      help="where the upgrade kit (firewall list, approval, run script) is written after a real prepare (default prepared)")
    prep.add_argument("--target", help="the version you are about to upgrade to: pre-download its images, and only touch firewalls it would change")
    prep.add_argument("--path-rules", help="path rules JSON (default: the packaged rules)")
    prep.add_argument("--out", help="directory for prepare.plan.json and prepare.approval.template.json")
    prep.set_defaults(func=_prepare)

    st = sub.add_parser("settings", help="show effective settings")
    st.add_argument("--settings")
    st.add_argument("--set", action="append")
    st.set_defaults(func=_settings)

    args = parser.parse_args(argv)
    if getattr(args, "panorama", None):
        args.source = "panorama:" + args.panorama
    if getattr(args, "insecure_tls", False):
        os.environ["PANUPGRADE_INSECURE_TLS"] = "1"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
