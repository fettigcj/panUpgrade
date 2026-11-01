# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Command line: ``select`` and ``plan`` (read-only), ``prepare`` and ``upgrade`` (real with a live source, a rehearsal with --dry-run or a fake source), ``settings``."""

import argparse
import getpass
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from dataclasses import asdict
from pathlib import Path

from . import (approval as approval_mod, fleetreport, console, executor, paths, prepare as prepare_mod, progress, releases as releases_mod, report,
               settings as settings_mod, xlsxout)
from . import credstore, profiles, site, trust
from .credstore import NeedsSignIn
from .trust import CertificateProblem
from .journal import Journal, JournalError
from .planner import PlanError, Selection, build_plan
from .simulator import SimBackend
from .sources import FakeFleet, SourceError, snapshot




_SAID = set()
DEFAULT_RELEASES = "releases.json"    # the PAN-OS release table saved by `releases --update`; used from the same folder when present
DEFAULT_SETTINGS = "settings.toml"   # saved by `settings`; used by every command run from the same folder unless --settings names another
DEFAULT_LIST = "upgradeList.txt"      # what `select` saves and plan / prepare / upgrade read when no firewalls are named


def _tilde(path):
    """A path as the person would type it: under their home folder it starts with ~."""
    full = Path(path).expanduser()
    try:
        return os.path.join("~", str(full.resolve().relative_to(Path.home().resolve())))
    except ValueError:
        return str(full)


def _where_from(origin, flag=None):
    """Words for where a sign-in value came from: a flag, an environment variable, the Panorama profile, or the default."""
    if flag:
        return f"set with {flag}"
    if origin.startswith("profile:"):
        return f"from the profile {origin[len('profile:'):]}"
    return "default location" if origin == "default" else f"from {origin}"


def _profile(args):
    """The Panorama profile in use, or None."""
    return getattr(args, "_profile", None)


def _ws(args, name):
    """A file or folder in the working place: the profile's workspace, or the current folder when no profile is in use."""
    profile = _profile(args)
    return profile.path(name) if profile else Path(name)


def _found_in(args):
    return f"found in the profile {_profile(args).name}" if _profile(args) else "found in this folder"


def _creds(args):
    """(credentials file path, where that choice came from)."""
    if args.creds_file:
        return args.creds_file, _where_from(None, "--creds-file")
    path, origin = site.creds_path(_profile(args))
    return path, _where_from(origin)


_KEYS = {}


def _login(args):
    """Settle, once, who reads and who changes. The site decides how it signs in; this only follows what it said.

    reading / changes are account names from the credentials file, or None where a login is typed at the prompt.
    With no account named: a credentials file holding exactly one account means that one; changes use the reading account.
    When something the person can fix is missing (no file, no account), a terminal gets the sign-in screen instead of an error.
    """
    if getattr(args, "_login", None):
        return args._login
    try:
        return _settle_login(args)
    except NeedsSignIn as exc:
        if _offer_sign_in_screen(args, exc):
            return _settle_login(args)
        raise


def _settle_login(args):
    ask = getattr(args, "ask_credentials", None)
    reading = changes = how = None
    if ask != "all":
        if args.account:
            reading, how = args.account, _where_from(None, "--account")
        elif site.account(_profile(args))[0]:
            reading, how = site.account(_profile(args))[0], _where_from(site.account(_profile(args))[1])
        else:
            path, _origin = _creds(args)
            names = sorted(credstore.read(path))
            if len(names) != 1:
                if not names:
                    raise NeedsSignIn(f"{path} holds no accounts yet")
                raise NeedsSignIn(f"{path} holds {len(names)} accounts ({', '.join(names)}) and none is chosen. Choose one with `panupgrade auth`, "
                                  f"say which with --account NAME (or PANUPGRADE_ACCOUNT), or use --ask-credentials to type a login instead")
            reading, how = names[0], "the only account in that file"
    if ask not in ("changes", "all"):
        changes = getattr(args, "work_account", None) or site.work_account(_profile(args))[0] or reading
    args._login = {"reading": reading, "changes": changes, "ask": ask, "how": how,
                   "changes_how": (_where_from(None, "--work-account") if getattr(args, "work_account", None)
                                   else _where_from(site.work_account(_profile(args))[1]) if site.work_account(_profile(args))[0] else "")}
    return args._login


def _offer_sign_in_screen(args, problem):
    """On a terminal, say what is missing and open the sign-in screen so it can be fixed on the spot. True when something was saved."""
    if getattr(args, "_in_shell", False):
        args._sign_in_problem = str(problem)                  # the shell has its own sign-in screen and takes the person there
        return False
    if ("sign-in screen",) in _SAID or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    _SAID.add(("sign-in screen",))
    style = _style(args)
    print(style.line("warn", f"Cannot sign in yet: {problem}."))
    print(style.line("info", "Opening the sign-in screen so you can fix that here. Quit it to stop."))
    saved = _run_auth_screen(args)
    if saved:
        try:
            args._profile = _resolve_profile(args)                 # the screen may have made the profile for this Panorama
        except profiles.ProfileError:
            pass
    return saved


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
        try:
            key = _trusting(args, lambda: key_from_account(address, _creds(args)[0], account))
        except NeedsSignIn as exc:
            if not _offer_sign_in_screen(args, exc):
                raise
            key = _trusting(args, lambda: key_from_account(address, _creds(args)[0], account))
        _KEYS[(address, account)] = (account, key)
    return _KEYS[(address, account)][1]


def _ask_to_trust(args, problem):
    """At a terminal, show the certificate's fingerprint and ask whether to trust exactly it. True when the person said yes."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or not problem.sha256:
        return False
    style = _style(args)
    print(style.line("warn", f"Cannot verify {problem.address}'s certificate: {problem.reason}."))
    print(f"  SHA-256 fingerprint: {trust.pretty(problem.sha256)}")
    print("  Compare it with the one Panorama shows (Device > Certificate Management > Certificates).")
    try:
        answer = input(f"Accept the risk and trust exactly this certificate for {problem.address} from now on? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer not in ("y", "yes"):
        return False
    trust.pin(problem.address, problem.sha256, problem.reason)
    print(style.line("ok", f"Trusting that certificate for {problem.address}. `panupgrade auth` can forget it."))
    return True


def _trusting(args, connect):
    """Run ``connect``. When Panorama's certificate cannot be verified, offer to accept it and try once more: at a terminal by asking,
    in the shell by handing the problem to the shell's sign-in screen. Anywhere else the error just goes through."""
    try:
        return connect()
    except CertificateProblem as problem:
        if getattr(args, "_in_shell", False):
            args._certificate_problem = problem
            raise
        if not _ask_to_trust(args, problem):
            raise
        return connect()


def _with_key(args, account):
    """', signs in with its stored API key' for an account kept as an API key in the credentials file; nothing for a password."""
    from panupgrade_live.panorama import stored_kind
    try:
        return ", signs in with its stored API key" if stored_kind(_creds(args)[0], account) == "API key" else ""
    except SourceError:
        return ""


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
            text = f"Changes will be made as: {login['changes']} ({login['changes_how']}{_with_key(args, login['changes'])})"
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
    login = _login(args)
    if login["ask"] == "all":
        print(style.line("info", "You will be asked for a username and password (used for reading and for changes; not stored)"))
        return
    creds, origin = _creds(args)
    print(style.line("info", f"Credentials file: {_tilde(creds)} ({origin})"))
    print(style.line("info", f"Reading as: {login['reading']} ({login['how']}{_with_key(args, login['reading'])})"))


def _load_settings(args):
    """Load the settings and say, once, where they came from."""
    found = ""
    if not args.settings and _ws(args, DEFAULT_SETTINGS).is_file():
        args.settings, found = str(_ws(args, DEFAULT_SETTINGS).resolve()), f" ({_found_in(args)})"     # the kit script then pins this exact file
    settings = settings_mod.load(args.settings, args.set)
    if _profile(args) and not os.path.isabs(settings.backup_dir):
        settings.backup_dir = str(_profile(args).path(settings.backup_dir))       # backups belong with the Panorama they came from
    if ("settings", args.settings) not in _SAID:
        _SAID.add(("settings", args.settings))
        style = _style(args)
        origin = f"settings file {_tilde(args.settings)}{found}" if args.settings else "built-in default settings (no settings file given)"
        extra = f", with {len(args.set)} changed on the command line" if args.set else ""
        print(style.line("info", f"Using {origin}{extra}"))
    return settings


def _source(args):
    spec, record = args.source, args.record
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
        key = _key_for(args, address, "reading")
        return _trusting(args, lambda: live_source(address, recorder=recorder, key=key))
    raise SystemExit("sources: fake:<file> or panorama:<address> (read-only)")


def _read_list(path):
    """The serial numbers in a list file: one per line, '#' starts a note."""
    serials = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()      # 'SERIAL   # a note' is fine
        if line:
            serials.append(line.split()[0])
    return serials


def _default_list(args):
    """The saved list to use when the command names no firewalls at all (None when it does, or when there is no saved list)."""
    if getattr(args, "command", None) not in ("plan", "prepare", "upgrade") or getattr(args, "all", False):
        return None
    if args.serial or args.serials_file or any(getattr(args, f, None) for f in _FILTERS):
        return None
    return _ws(args, DEFAULT_LIST) if _ws(args, DEFAULT_LIST).is_file() else None


def _serials(args):
    serials = list(args.serial or [])
    path = args.serials_file
    default = _default_list(args)
    if default:
        path = default
        if ("list", str(default)) not in _SAID:
            _SAID.add(("list", str(default)))
            found = _read_list(default)
            print(_style(args).line("info", f"Using the firewall list {_tilde(default.resolve())} ({len(found)} firewalls). "
                                            f"Name others with --serials-file, or use --all for every firewall Panorama manages."))
    if path:
        serials += _read_list(path)
    return serials


def _selection(args):
    return Selection(serials=_serials(args), name_glob=args.name or "", name_contains=args.name_contains or "",
                     models=args.model or [], device_groups=args.device_group or [], tags=args.tag or [],
                     version_is=args.version_is or "", version_older_than=args.version_older_than or "",
                     version_newer_than=args.version_newer_than or "", ha_states=args.ha_state or [],
                     accept_standalone_outage=getattr(args, "accept_standalone_outage", False))


def _style(args):
    return console.Style.detect(getattr(args, "color", "auto"), getattr(args, "ascii", False))


def _load_releases(args):
    """The PAN-OS release table: the file named, else ./releases.json when it exists, else the one that ships with the tool.

    Says which, once. A table found in the folder is recorded in ``args.releases`` so the upgrade script pins that exact file.
    """
    found = ""
    if not args.releases and _ws(args, DEFAULT_RELEASES).is_file():
        args.releases, found = str(_ws(args, DEFAULT_RELEASES).resolve()), f" ({_found_in(args)})"
    table = releases_mod.load(args.releases)
    if ("releases", args.releases) not in _SAID:
        _SAID.add(("releases", args.releases))
        where = f"release table {_tilde(args.releases)}{found}" if args.releases else "the release table that ships with the tool"
        when = f", fetched {table.fetched}" if table.fetched else ""
        print(_style(args).line("info", f"Using {where}{when}"))
    return table


def _build(args, source=None):
    settings = _load_settings(args)
    rules = paths.load_rules(args.path_rules) if args.path_rules else None
    table = _load_releases(args)
    source = source or _source(args)
    return build_plan(source, args.target, settings, _selection(args), rules, releases=table), settings


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


def _repeat_hint(args, flags):
    """How to get the list the form just built without the form, or why that is not short enough to print."""
    if len([f for f in flags if f[0] == "--serial"]) > 8:
        if args.out:
            return f"This list is saved in {args.out}; use it in later commands with --serials-file {args.out}"
        return "This is a hand-picked list. Put a file name in 'Save the list to' next time to keep it for the later commands."
    return "To get this same list without the form: " + _command_line(args, flags + ([("--out", args.out)] if args.out else []))


def _write_list(path, chosen, criteria):
    path = Path(path).expanduser()
    lines = [f"# panupgrade device list - criteria: {criteria}",
             "# One serial per line; text after # is ignored. Delete a line to drop a device; the file is the list that is used."]
    lines += [f"{r.serial}   # {r.hostname}  {r.sw_version}  {r.ha_state or '-'}" for r in chosen]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _default_list_text(args):
    """Where the list is kept by default, as the person would type it: a plain file name, or the path in the profile's workspace."""
    return _tilde(_ws(args, DEFAULT_LIST)) if _profile(args) else DEFAULT_LIST


def _select_form(args, refs):
    """The select form for these firewalls, starting from the list saved last time when no firewalls were named."""
    from . import selectform
    selection = _selection(args)
    saved = args.out or _default_list_text(args)
    notice = ""
    if not selection.serials and Path(saved).expanduser().is_file():           # come back to the list you made last time
        selection.serials = _read_list(saved)
        notice = f"Started with the {len(selection.serials)} firewalls already in {saved}."

    def save_list(form):
        path = form.out.strip()
        _write_list(path, form.included_refs(), form.describe())
        return f"Saved {len(form.included)} firewall{'s' if len(form.included) != 1 else ''} to {path}."
    return selectform.Form(refs, selection, saved, notice, save_list)


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
        form = _select_form(args, refs)
        if not selectform.show(form, f"panupgrade select - {len(refs)} firewalls from {_where(args)}"):
            print("Cancelled; nothing was selected.")
            return 1
        chosen, criteria, args.out = form.included_refs(), form.describe(), form.out.strip() or None
        print(style.line("info", _repeat_hint(args, form.flags())))
    else:
        chosen, criteria = [r for r in refs if selection.matches(r)], selection.describe()
    rows = [[i, r.hostname, r.serial, r.model, r.sw_version, r.ha_state or "-", r.device_group or "-",
             "yes" if r.connected else "NO"] for i, r in enumerate(chosen, 1)]
    print(style.bold(f"Criteria: {criteria}"))
    sys.stdout.write(console.render_table(["#", "hostname", "serial", "model", "PAN-OS", "HA", "device group", "online"], rows, style))
    print(style.line("info", f"{len(chosen)} of {len(refs)} devices match."))
    home_list = _ws(args, DEFAULT_LIST)
    where = f"for the profile {_profile(args).name}" if _profile(args) else "when you run them from this folder"
    if args.out:
        print(style.line("info", f"Saving the list to {args.out}. Check it and edit it if needed; " + (
            f"plan, prepare and upgrade use it automatically {where}." if Path(args.out).expanduser().resolve() == home_list.resolve()
            else f"pass it to the later commands with --serials-file {args.out}.")))
    else:
        print(style.line("info", f"To keep this list, add --out {_default_list_text(args)}; plan, prepare and upgrade then use it automatically {where}."))
    if args.out:
        _write_list(args.out, chosen, criteria)
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
    try:
        settings, table = _load_settings(args), _load_releases(args)
    except (settings_mod.SettingsError, SourceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = fleetreport.build(snap, settings, table)
    sys.stdout.write(fleetreport.render(report, style, args.source.split(":", 1)[-1] if args.source.startswith("panorama:") else ""))
    if args.out:
        Path(args.out).write_text(json.dumps({"panorama_version": report["panorama_version"], "versions": report["versions"],
                                              "attention": report["attention"], "advice": report["advice"], "devices": report["devices"]}, indent=1) + chr(10), encoding="utf-8")
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
    runs = Path(args.runs_dir) if args.runs_dir else _ws(args, "runs")
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
    return console.EventPrinter(style, names, out=progress.output()), (lambda: None)       # (the stream is looked up here: the run's worker threads print through it)


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
    return _new_approval(plan_hash, serials, acks, journal_path, "typed APPROVE at the terminal")


def _new_approval(plan_hash, serials, acks, journal_path, how):
    """The record of a person's go-ahead, bound to one plan and written next to the journal so it is as auditable as a file handed in."""
    approval = approval_mod.Approval(plan_hash, list(serials), list(acks), f"{getpass.getuser()} ({how})",
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
            read_api = PanoramaApi(address, _key_for(args, address, "reading"), limiter=limiter)
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


class UpgradeBundle:
    """What the review of an upgrade found, kept for the run that follows: the plan, the connection to carry it out, and any earlier run of
    this exact plan that did not finish. ``stop`` asks the run to start nothing new (steps already going finish)."""

    def __init__(self, plan, settings, backend, fleet, label, previous):
        self.plan, self.settings, self.backend, self.fleet, self.label, self.previous = plan, settings, backend, fleet, label, previous
        self.created, self.stop = time.time(), threading.Event()
        self.events, self._events_lock = [], threading.Lock()            # [(time, journal record)] of the run, for the screen to sort by firewall

    def record(self, record):
        with self._events_lock:
            self.events.append((time.time(), record))

    def events_snapshot(self):
        with self._events_lock:
            return list(self.events)

    def ready(self):
        return [d for d in self.plan["devices"] if d["verdict"] in ("ready", "caution")]

    def acks(self):
        return [f"{c['scope']}:{c['code']}" for c in self.plan["cautions"]]


def _upgrade(args):
    style = _style(args)
    return _guarded(style, lambda: _upgrade_execute(args, style, _upgrade_review(args, style)))


def _upgrade_review(args, style, show=False):
    """Read the fleet and build the upgrade plan. Changes nothing. With ``show`` the plan is printed as `plan` prints it."""
    backend, fleet, label = _backend_and_source(args)
    plan, settings = _build(args, fleet)
    runs = Path(args.runs_dir) if args.runs_dir else _ws(args, "runs")
    previous = None if args.dry_run else _unfinished_run(runs, "upgrade", plan)
    if show:
        sys.stdout.write(console.render_plan(plan, style))
        if previous is not None:
            print()
            print(style.line("warn", f"an earlier run of this exact plan did not finish: {previous[1]} ({previous[2]})"))
    return UpgradeBundle(plan, settings, backend, fleet, label, previous and (str(previous[1]), previous[2]))


def _upgrade_execute(args, style, bundle, approve=None):
    """Carry out a reviewed upgrade. ``approve(journal_path)`` returns the Approval (or None to stop); by default the person is asked at the terminal.
    When it is given the journal is chosen first, so the approval is recorded next to it, and the keyboard is never read."""
    plan, settings, backend, label = bundle.plan, bundle.settings, bundle.backend, bundle.label
    approval = None
    if args.dry_run and not args.approval:
        approval = approval_mod.Approval(plan["hash"], [d["serial"] for d in bundle.ready()], bundle.acks(),
                                         "dry run: nothing changes, so nothing was asked", "")
        print(style.line("info", "dry run: nothing changes, so no approval is asked"))
    elif args.approval:
        approval = approval_mod.Approval.from_file(args.approval)
        _print_cautions(style, plan, "Proceeding on the approval's acceptance of these cautions:", approval.acks)
    elif approve is None:
        ready = bundle.ready()
        approval = _interactive_approval(
            style, plan["hash"], [d["serial"] for d in ready], bundle.acks(),
            [f"{d['hostname']} ({d['serial']})  {d['current_version']} -> {plan['target']}" for d in ready], args.journal,
            [f"[{c['code']}] {c['scope']}: {c['text']}" for c in plan["cautions"]])
        if approval is None:
            print("No approval given. Run `panupgrade prepare` first (it writes the approval and a run script), "
                  "or re-run this in a terminal and type APPROVE, or pass --approval FILE.")
            return 3
    journal_path, resuming = _choose_journal(args, plan, "upgrade", style, tty=None if approve is None else False)
    if approval is None:
        approval = approve(journal_path)
        if approval is None:
            return 3
    journal = (Journal.resume if resuming else Journal.create)(journal_path, plan["hash"])
    print(style.line("info", f"journal: {journal_path}" + (" (continuing)" if resuming else "")))
    names = {d["serial"]: d["hostname"] for d in plan["devices"]}
    journal.listener, close_view = _listener(style, names, [s for unit in plan["order"] for s in unit], settings)
    printer = journal.listener

    def listener(record):                       # the screen keeps every event too, so it can show them firewall by firewall
        bundle.record(record)
        printer(record)
    journal.listener = listener
    if label == "LIVE":
        print(style.line("stop", f"LIVE RUN - this will change real firewalls: {len(plan['order'])} unit(s), up to {settings.max_parallel} at a time, journal {journal_path}"))
    else:
        print(style.line("info", f"{label} - no write command is sent to any real device; the run is rehearsed against a model"
                         + (" seeded from live state" if label == "DRY RUN" else "")))
    try:
        result = executor.run(plan, approval, backend, journal, settings, stop=bundle.stop)
    except executor.ExecutionRefused:
        _discard_if_empty(journal, resuming)
        raise
    finally:
        close_view()
    sys.stdout.write(console.render_outcomes(result, names, style))
    return 0 if result["failures"] == 0 else 1


class PrepareBundle:
    """What the review of a prepare found, kept for the run that follows: the plan, who is who, and the connection to carry it out."""

    def __init__(self, plan, names, backend, fleet, label, settings):
        self.plan, self.names, self.backend, self.fleet, self.label, self.settings = plan, names, backend, fleet, label, settings
        self.created, self.stop = time.time(), threading.Event()       # stop: start no new task (those going finish)


def _guarded(style, fn):
    """Run one phase of prepare or upgrade and turn the trouble a person can act on into a message and an exit code."""
    try:
        return fn()
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
    except (settings_mod.SettingsError, prepare_mod.PrepareError, PlanError, SourceError, JournalError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _prepare(args):
    style = _style(args)
    return _guarded(style, lambda: _prepare_execute(args, style, _prepare_review(args, style)))


def _prepare_heading(plan):
    """What the approval is told it is approving; content updates are said plainly because they land outside the person's own change schedule."""
    if any(u["content"] for u in plan["units"]):
        return ("Approval needed. This includes dynamic CONTENT updates, installed on the firewalls NOW and therefore outside your "
                "own change schedule for them (the config sync cannot happen until content is level):")
    return "Approval needed. You are being asked to approve exactly this:"


def _prepare_review(args, style):
    """Read the fleet and say what prepare would do. Changes nothing. Returns a PrepareBundle for _prepare_execute."""
    settings = _load_settings(args)
    rules = paths.load_rules(args.path_rules) if args.path_rules else None
    _load_releases(args)                                  # so the upgrade script pins the same table
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
    for s_ in plan["skipped"]:
        print(style.line("skip", f"{s_['hostname']}: {s_['why']}"))
    return PrepareBundle(plan, names, backend, fleet, label, settings)


def _prepare_execute(args, style, bundle, approve=None):
    """Carry out a reviewed prepare. ``approve(journal_path)`` returns the Approval (or None to stop); by default the person is asked at the terminal."""
    plan, names, backend, label, settings = bundle.plan, bundle.names, bundle.backend, bundle.label, bundle.settings
    result = None
    if plan["serials"]:
        result = _prepare_tasks(args, style, plan, names, backend, settings, label, approve, bundle.stop)
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


def _prepare_tasks(args, style, plan, names, backend, settings, label, approve=None, stop=None):
    """Approve and run the preparation tasks (config sync, image downloads). Returns the result, or an exit code."""
    if args.dry_run and not args.approval:
        approval = approval_mod.Approval(plan["hash"], list(plan["serials"]), [], "dry run: nothing changes, so nothing was asked", "")
        print(style.line("info", "dry run: nothing changes, so no approval is asked"))
    elif args.approval:
        approval = approval_mod.Approval.from_file(args.approval)
    else:
        journal_path, resuming = _choose_journal(args, plan, "prepare", style)
        if approve is not None:
            approval = approve(journal_path)
        else:
            approval = _interactive_approval(style, plan["hash"], plan["serials"], [], prepare_mod.describe(plan), journal_path,
                                             heading=_prepare_heading(plan))
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
        return prepare_mod.run(plan, approval, backend, journal, settings, stop=stop)
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
        if note.get("devices"):
            print(style.line("info", f"{style.bold(note['title'])} ({len(note['devices'])})", 2))
            print(style.bullet(note["text"], 6))
            print("\n".join(report.device_rows(note["devices"], "        ")))
        else:
            print(style.line("info", f"{note['scope']}: {note['text']}", 2))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.kit_dir = args.kit_dir or str(_ws(args, "prepared"))
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


def _wants_settings_form(args):
    """The form opens when asked for (-i), and by itself when `settings` is typed at a terminal with nothing else to say."""
    if args.interactive is False:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        if args.interactive:
            raise SourceError("--interactive needs a terminal; without one, `settings` prints the current values")
        return False
    return bool(args.interactive) or not args.set


def _settings_form(args):
    """The settings form, on the settings file in use (or the one it would create), with any --set values applied on top."""
    from . import settingsform
    target = args.settings or str(_ws(args, DEFAULT_SETTINGS))
    file = args.settings or (target if Path(target).is_file() else None)
    on_disk, current = settings_mod.load(file), settings_mod.load(file, args.set)
    notice = (f"Editing {_tilde(Path(target).resolve())}. Saving rewrites it with only the settings that differ from the defaults; comments are not kept."
              if Path(target).is_file() else
              f"No settings file yet: this starts from the built-in defaults and saves to {_tilde(Path(target).resolve())}.")

    def save(form, chosen):
        settingsform.write_file(target, chosen)
        return f"Saved {form.changed_count()} changed setting{'s' if form.changed_count() != 1 else ''} to {target}."
    return settingsform.Form(current, target, save, notice, baseline=on_disk)


def _settings(args):
    style = _style(args)
    try:
        use_form = _wants_settings_form(args)
        target = args.settings or str(_ws(args, DEFAULT_SETTINGS))
        file = args.settings or (target if Path(target).is_file() else None)
        on_disk = settings_mod.load(file)
        current = settings_mod.load(file, args.set)
    except (settings_mod.SettingsError, SourceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not use_form:
        print(json.dumps(asdict(current), indent=2))
        for warning in current.loosening_warnings():
            print(f"caution: {warning}", file=sys.stderr)
        return 0
    from . import tui
    form = _settings_form(args)
    if not tui.show(form, f"panupgrade settings - {_tilde(Path(target).resolve())}"):
        print("Cancelled; nothing was saved." if form.dirty() else "Closed; nothing changed.")
        return 1
    chosen = form.settings()
    print(style.line("ok", f"Settings saved to {_tilde(Path(target).resolve())} ({form.changed_count()} differ from the defaults)."))
    if args.settings is None:
        print(style.line("info", "Every command run from this folder now uses it; name another file with --settings."))
    for warning in chosen.loosening_warnings():
        print(style.line("warn", warning))
    return 0


def _releases(args):
    """Show the PAN-OS release table in use, or (--update) download a fresh one into a file you own."""
    from panupgrade_live import releasefeed
    style = _style(args)
    target = args.file or str(_ws(args, DEFAULT_RELEASES))
    try:
        if args.update:
            try:
                before = {e["release"] for e in releases_mod.load(target if Path(target).is_file() else None).entries}
            except releases_mod.ReleaseError:
                before = set()
            fresh = releasefeed.download()
            releases_mod.save(target, fresh)
            added = sorted((e["release"] for e in fresh.entries if e["release"] not in before), key=releases_mod.key)
            print(style.line("ok", f"Saved {len(fresh.entries)} PAN-OS releases to {_tilde(Path(target).resolve())} (from {releases_mod.URL})."))
            print(style.line("info", ("New since the table you had: " + ", ".join(added) + ".") if before and added else
                             "Nothing new since the table you had." if before else "That is now the table every command run from this folder uses."))
            return 0
        path = args.file or (target if Path(target).is_file() else None)
        table = releases_mod.load(path)
    except (releases_mod.ReleaseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    where = _tilde(Path(path).resolve()) if path else "the table that ships with the tool"
    print(style.bold(f"PAN-OS releases, from {where}" + (f" (fetched {table.fetched})" if table.fetched else "")))
    rows = [[e["release"], e.get("released") or "-", e.get("standard_eol") or "-", e.get("extended_eol") or "-", releases_mod.standing(e)]
            for e in reversed(table.entries)]
    sys.stdout.write(console.render_table(["release", "released", "standard support ends", "extended support ends", "today"], rows, style))
    print(style.line("info", "'Minor' distances in the settings are counted along this list, newest first: each row is one step. "
                             "Dates are the vendor's, copied as they were; check them against the vendor before relying on one."))
    if table.fetched and (date.today() - date.fromisoformat(table.fetched)).days > 180:
        print(style.line("warn", "This table is more than six months old. `panupgrade releases --update` fetches a fresh one."))
    if not path:
        print(style.line("info", "`panupgrade releases --update` saves a fresh copy here, in ./releases.json, which you can also edit by hand."))
    return 0


class _AuthServices:
    """What the sign-in screen needs from the network and the disk; kept out of the form so the form can be tested without either."""

    def sign_in(self, address, user, password):
        from panupgrade_live.panorama import key_from_login
        return key_from_login(address, user, password)

    def test(self, address, name, secret):
        from panupgrade_live.panorama import PanoramaApi, key_from_login
        key = secret["api_key"].strip() if isinstance(secret, dict) else key_from_login(address, name, secret)
        info = PanoramaApi(address, key).op("show system info")
        version = (info.findtext("result/system/sw-version") or "").strip()
        return True, f"{name} signed in to {address}" + (f" (Panorama {version})." if version else ".")

    def save(self, form):
        from . import authform
        return authform.save_all(form)

    def trust(self, address, sha256, reason):
        trust.pin(address, sha256, reason)

    def forget(self, address):
        return trust.forget(address)

    def trusted(self, address):
        return trust.record(address)


def _auth_form(args):
    """(form, None) for all the profiles, opened on the one in use, or (None, why) when a credentials file exists but cannot be understood."""
    from . import authform, userenv
    entries = []
    for name in profiles.names():
        profile = profiles.load(name)
        path = profile.creds_file
        try:
            accounts = credstore.read(path) if Path(path).is_file() else {}
        except SourceError as exc:
            return None, f"{exc}. Fix or remove that file by hand; the sign-in screen will not overwrite what it cannot read."
        entries.append(authform.Entry(name, profile.address, accounts, profile.account, profile.work_account, path,
                                      sign_ins=profiles.sign_ins(name)))
    store = "env" if userenv.supported() and userenv.get("PANUPGRADE_PROFILE") else "state"
    overrides = [name for name in authform.ACCOUNT_ENV if os.environ.get(name)]
    active = _profile(args).name if _profile(args) else (profiles.remembered() or "")
    source = getattr(args, "source", None) or ""
    address = source[len("panorama:"):] if source.startswith("panorama:") else ""
    if os.environ.get("PANUPGRADE_PROFILE") and os.environ["PANUPGRADE_PROFILE"] != active:
        overrides.append("PANUPGRADE_PROFILE")
    form = authform.Form(entries, active, store, "", _AuthServices(), overrides, address)
    return form, None


def _run_auth_screen(args):
    """Open the sign-in screen. True when something was saved."""
    from . import tui
    form, problem = _auth_form(args)
    if form is None:
        print(_style(args).line("fail", problem))
        return False
    accepted = tui.show(form, "panupgrade auth - Panorama profiles and their admin accounts")
    return accepted or form.ever_saved


def _auth(args):
    """Set up Panorama profiles and their admin accounts: a form on a terminal, a plain summary otherwise."""
    style = _style(args)
    if sys.stdin.isatty() and sys.stdout.isatty():
        saved = _run_auth_screen(args)
        print(style.line("ok" if saved else "info", "Sign-in settings saved." if saved else "Nothing was saved."))
        return 0 if saved else 1
    print(style.bold("Panorama profiles"))
    names = profiles.names()
    if not names:
        print("  none yet; run `panupgrade auth` at a terminal to make one")
    current = _profile(args)
    for name in names:
        profile = profiles.load(name)
        print(f"  {name}{' (in use)' if current and current.name == name else ''}: {profile.address or 'no address yet'}")
        try:
            accounts = credstore.read(profile.creds_file) if Path(profile.creds_file).is_file() else {}
        except SourceError as exc:
            print(f"    error: {exc}", file=sys.stderr)
            return 2
        for account in sorted(accounts):
            roles = [w for w, n in (("reads", profile.account), ("changes", profile.work_account)) if n == account]
            print(f"    account {account}: {credstore.kind(accounts[account])}" + (f" ({', '.join(roles)})" if roles else ""))
        if not accounts:
            print("    no accounts yet")
    return 0


def _shell(args):
    """The whole workflow on one screen (see ``shell``). Needs a terminal."""
    from . import shell, tui
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: the shell needs a terminal; use the commands (see `panupgrade help`) in a script", file=sys.stderr)
        return 2
    try:
        profile = _resolve_profile(args)
    except profiles.ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    remembered = site.read_state()
    address = getattr(args, "panorama", None) or (profile.address if profile else remembered.get("panorama", ""))
    target = getattr(args, "target", None) or (profile.target if profile else remembered.get("target", ""))
    session = shell.Session(args, address, target, profile)
    tui.show(shell.Shell(session), "panupgrade")
    session.persist()
    if session.profile:
        profiles.remember(session.profile.name)
    print(_style(args).line("info", "Closed the panupgrade screen. Nothing was changed on any firewall."))
    return 0


def _selection_args(p):
    """Connection options and the filters that choose which firewalls a command looks at (all of them by default)."""
    conn = p.add_argument_group("connect to Panorama")
    where = conn.add_mutually_exclusive_group()
    where.add_argument("--panorama", metavar="ADDRESS", help="the Panorama that manages your firewalls (name or IP address); "
                                                           "optional when a profile names it")
    conn.add_argument("--profile", metavar="NAME", help="the Panorama profile to work in: its address, accounts, firewall list, settings and "
                                                       "records (or PANUPGRADE_PROFILE; see `panupgrade auth`)")
    where.add_argument("--source", help="fake:<fleet.json> to try the tool on an invented fleet, with no firewall involved")
    conn.add_argument("--creds-file", help="credentials file: accounts with a password or API key each (default: ~/.panupgrade/credentials.txt, "
                                           "or PANUPGRADE_CREDS, or the choice made in `panupgrade auth`); never printed")
    conn.add_argument("--account", metavar="NAME",
                      help="the account in the credentials file to sign in with (or PANUPGRADE_ACCOUNT, or the choice made in `panupgrade auth`). "
                           "Optional when the file holds exactly one account")
    conn.add_argument("--ask-credentials", choices=["changes", "all"],
                      help="type a username and password instead of reading them from a file: 'changes' for the steps that change "
                           "firewalls (reading still uses the file), 'all' for everything. Never stored or printed")
    conn.add_argument("--insecure-tls", action="store_true",
                      help="do not verify Panorama's TLS certificate (lab with a self-signed certificate only; or set PANUPGRADE_INSECURE_TLS=1)")
    conn.add_argument("--record", help="directory to save each raw device response, to build test fixtures")
    pick = p.add_argument_group("choose which firewalls (filters combine; with none, every firewall)")
    pick.add_argument("--serial", action="append", help="this serial (repeatable)")
    pick.add_argument("--serials-file", help="file with one serial per line ('#' starts a comment); the output of `select`. "
                                             f"Without any firewall named, plan / prepare / upgrade use ./{DEFAULT_LIST} if it exists")
    pick.add_argument("--all", action="store_true", help=f"every firewall Panorama manages, even if ./{DEFAULT_LIST} exists")
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
  auth      set up how the tool signs in to Panorama (accounts, API keys)
  settings  change the settings on a form (prints them when there is no terminal)
  releases  the list of PAN-OS releases used to count minor versions; --update downloads a fresh one
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
    p.add_argument("--releases", help=f"PAN-OS release table JSON (default ./{DEFAULT_RELEASES} if present, else the one that ships with the tool; "
                                      f"`panupgrade releases --update` makes a fresh one)")
    p.add_argument("--accept-standalone-outage", action="store_true",
                   help="include standalone firewalls (their reboot is an outage)")


def _run_args(p, journal_required=False):
    p.add_argument("--journal", required=journal_required,
                   help="override the run file; by default a new, time-stamped one is named for you under --runs-dir and never overwritten")
    p.add_argument("--runs-dir", help="where run journals are kept (default: runs, in the profile's workspace or the current folder)")
    p.add_argument("--resume", action="store_true", help="continue the unfinished earlier run of this exact plan (or the file named by --journal)")
    p.add_argument("--fresh", action="store_true", help="start a new run even if an unfinished earlier run of this plan exists")
    p.add_argument("--work-account", metavar="NAME",
                   help="a different account in the credentials file for the steps that change firewalls (or PANUPGRADE_WORK_ACCOUNT, or the choice "
                        "made in `panupgrade auth`). Default: the same account as --account")
    p.add_argument("--dry-run", action="store_true",
                   help="with a live source: read it once and rehearse against a model of it. WITHOUT this flag a live source "
                        "means a REAL run: it changes the firewalls")
    p.add_argument("--sim-fail", action="append", help="inject a failure: serial:operation (e.g. SIMA0001:install)")


def build_parser():
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
    rep.add_argument("--settings", help="settings TOML file (spread_limit and panorama_compare decide which firewalls are called behind Panorama)")
    rep.add_argument("--set", action="append", help="override a setting: name=value")
    rep.add_argument("--releases", help=f"PAN-OS release table JSON (default ./{DEFAULT_RELEASES} if present, else the one that ships with the tool)")
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
    prep.add_argument("--settings", help="settings TOML file (sync_to_peer must be Both for prepare to sync a pair)")
    prep.add_argument("--set", action="append", help="override a setting: name=value")
    prep.add_argument("--approval", help="approval JSON for the preparation tasks themselves; without it you are asked to type APPROVE "
                                         "(a --dry-run asks nothing)")
    prep.add_argument("--kit-dir",
                      help="where the upgrade kit (firewall list, approval, run script) is written after a real prepare (default prepared)")
    prep.add_argument("--target", help="the version you are about to upgrade to: pre-download its images, and only touch firewalls it would change")
    prep.add_argument("--path-rules", help="path rules JSON (default: the packaged rules)")
    prep.add_argument("--releases", help=f"PAN-OS release table JSON (default ./{DEFAULT_RELEASES} if present, else the shipped one)")
    prep.add_argument("--out", help="directory for prepare.plan.json and prepare.approval.template.json")
    prep.set_defaults(func=_prepare)

    sh = sub.add_parser("shell", help="the whole workflow on one screen (this is also what plain `panupgrade` opens at a terminal)")
    sh.add_argument("--panorama", metavar="ADDRESS", help="the Panorama to work with (remembered from last time when left out)")
    sh.add_argument("--target", help="the PAN-OS version to upgrade to (remembered with the profile when left out)")
    sh.add_argument("--profile", metavar="NAME", help="the Panorama profile to start in (the one used last, when left out)")
    sh.add_argument("--creds-file", help="the credentials file to use (default ~/.panupgrade/credentials.txt, or PANUPGRADE_CREDS)")
    sh.add_argument("--insecure-tls", action="store_true", help="do not verify Panorama's TLS certificate (lab use only)")
    sh.set_defaults(func=_shell)

    au = sub.add_parser("auth", help="set up how the tool signs in to Panorama: accounts, API keys, which account reads and which changes")
    au.add_argument("--panorama", metavar="ADDRESS", help="the Panorama to test a sign-in against, or to obtain an API key from")
    au.add_argument("--profile", metavar="NAME", help="the Panorama profile to open on")
    au.add_argument("--creds-file", help="the credentials file to edit (default ~/.panupgrade/credentials.txt, or PANUPGRADE_CREDS)")
    au.add_argument("--insecure-tls", action="store_true", help="do not verify Panorama's TLS certificate (lab use only)")
    au.add_argument("--color", choices=["auto", "always", "never"], default="auto", help=argparse.SUPPRESS)
    au.add_argument("--ascii", action="store_true", help=argparse.SUPPRESS)
    au.set_defaults(func=_auth)

    rl = sub.add_parser("releases", help="show the PAN-OS release table used to count minor versions, or download a fresh one")
    rl.add_argument("--update", action="store_true", help=f"download the vendor's list of PAN-OS releases and save it as ./{DEFAULT_RELEASES}")
    rl.add_argument("--file", help=f"the table to show, or to save to with --update (default {DEFAULT_RELEASES} in the profile's workspace or the current folder)")
    rl.add_argument("--profile", metavar="NAME", help="the Panorama profile whose release table to show or update")
    rl.add_argument("--color", choices=["auto", "always", "never"], default="auto", help=argparse.SUPPRESS)
    rl.add_argument("--ascii", action="store_true", help=argparse.SUPPRESS)
    rl.set_defaults(func=_releases)

    st = sub.add_parser("settings", help="change the settings on a form (or print them when there is no terminal); changes nothing else")
    st.add_argument("--settings", help=f"the settings file to edit (default {DEFAULT_SETTINGS} in the profile's workspace or the current folder)")
    st.add_argument("--profile", metavar="NAME", help="the Panorama profile whose settings to edit")
    st.add_argument("--set", action="append", help="try a one-off value: name=value. With this, the settings are printed instead of editing a file")
    st.add_argument("-i", "--interactive", action=argparse.BooleanOptionalAction, default=None,
                    help="edit the settings on a form. Opens by itself when you run `settings` at a terminal; --no-interactive prints the values instead")
    st.add_argument("--color", choices=["auto", "always", "never"], default="auto", help=argparse.SUPPRESS)
    st.add_argument("--ascii", action="store_true", help=argparse.SUPPRESS)
    st.set_defaults(func=_settings)

    return parser


def _resolve_profile(args):
    """The Panorama profile this command works in (see ``profiles``), or None."""
    source = getattr(args, "source", None) or ""
    fake = source.startswith("fake:")
    address = getattr(args, "panorama", None) or (source[len("panorama:"):] if source.startswith("panorama:") else None)
    if fake and not getattr(args, "profile", None):
        return None
    return profiles.choose(getattr(args, "profile", None), address, use_remembered=not address)


def _use_profile(parser, args):
    """Settle which Panorama profile this command works in and let it supply the address when none was given."""
    address = getattr(args, "panorama", None)
    try:
        profile = _resolve_profile(args)
    except profiles.ProfileError as exc:
        parser.error(str(exc))
    args._profile = profile
    if profile is not None and hasattr(args, "panorama") and not address and not getattr(args, "source", None) and profile.address:
        args.panorama = profile.address


def parse_args(argv):
    """argv parsed as the command line would be, with --panorama folded into the source and --insecure-tls applied."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "source", None) and not args.source.startswith("fake:"):
        parser.error("--source takes fake:<fleet.json>; use --panorama ADDRESS for a real Panorama")
    _use_profile(parser, args)
    if getattr(args, "panorama", None):
        args.source = "panorama:" + args.panorama
    if hasattr(args, "source") and not args.source:
        parser.error("name a Panorama: --panorama ADDRESS, or --profile NAME (or make a profile with `panupgrade auth`)")
    if getattr(args, "insecure_tls", False):
        os.environ["PANUPGRADE_INSECURE_TLS"] = "1"
    return args


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:                                              # just `panupgrade`: the whole workflow on one screen, or the overview when there is no terminal
        if sys.stdin.isatty() and sys.stdout.isatty():
            return _shell(parse_args(["shell"]))
        return _help(argparse.Namespace(topic=None))
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
