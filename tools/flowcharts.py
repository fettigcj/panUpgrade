# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Draw what each panupgrade verb does, from the source code itself (nothing here is written by hand about the code).

    python tools/flowcharts.py            # writes docs/flow/verbs.md and docs/flow/checks.md
    python tools/flowcharts.py --check    # exits 1 when the committed files are out of date

Standard library only. The diagrams are Mermaid, which GitHub renders in Markdown.

How it works: every module under src/ is parsed with ``ast``. A call graph is built from the calls it can resolve (a name in the same
module, ``module.function``, ``self.method``, and a method called on some object when only a few classes define that name; those are
drawn dashed because the real class is only known at run time). Verbs and their handlers are read from ``build_parser`` in cli.py; the
checks from ``DEVICE_CHECKS`` in checks.py; what is read from each firewall from ``collect_facts`` in xmlops.py.

What it cannot see: calls made through callbacks and hooks (the one-screen shell hands work to the other verbs that way), and anything
decided by data. Treat a missing edge as "not visible to a static reading", not as "never happens".
"""

import argparse
import ast
import builtins
import io
import pathlib
import re
import sys
import threading
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUT = ROOT / "docs" / "flow"

# Modules that only present things (screens, files, text). Their functions are not drawn as steps of a verb.
PRESENTATION = {"console", "progress", "report", "runlog", "xlsxout", "authform", "settingsform", "selectform", "shell", "tui", "trust",
                "profiles", "credstore", "userenv", "site", "kit", "fleetreport", "approval", "journal", "model"}
# Not drawn as steps of a verb either: small helpers called from everywhere.
HELPERS = {"_style", "_ws", "_profile", "_found_in", "_creds", "_login", "_load_settings", "_where", "_command_line", "_repeat_hint",
           "_source", "_announce_connection", "_key_for", "_typed", "_trusting", "_with_key", "_tilde", "_settle_login", "_ask_to_trust",
           "_offer_sign_in_screen", "_guarded", "_selection", "_serials", "_default_list", "_read_list", "_default_list_text", "_write_list",
           "_wants_form", "_select_form", "_load_releases", "_print_cautions", "_new_approval", "_discard_if_empty", "_listener",
           "_write_run_log", "_interactive_approval", "_backend_and_source", "_use_profile", "parse_args", "build_parser", "main"}
# Where the tool's own work is done (and cli.py, which strings it together): a function in one of these is opened up to show what it calls.
DOMAIN = {"planner", "prepare", "executor", "checks", "sources", "xmlops", "paths", "releases", "simulator", "backend", "panorama"}
MAX_DEPTH = 5
AMBIGUOUS_MAX = 3            # a method name defined by more classes than this is not guessed at
_PLAIN = set()
for _kind in (list, dict, set, str, bytes, tuple, io.StringIO, pathlib.Path, threading.Event, threading.Lock, int, float, object):
    _PLAIN |= set(dir(_kind))
_PLAIN |= {"get", "items", "append", "add", "join", "format", "write", "read", "close", "start", "stop", "run", "update", "pop", "sort",
           "strip", "split", "lower", "upper", "replace", "startswith", "endswith", "record", "listen", "flush", "copy", "setdefault"}


# ---------------------------------------------------------------- the source, parsed

class Func:
    def __init__(self, module, qual, node, cls):
        self.module, self.qual, self.node, self.cls = module, qual, node, cls
        self.key = f"{module}.{qual}"
        doc = ast.get_docstring(node) or ""
        self.doc = doc.strip().splitlines()[0] if doc.strip() else ""


class Index:
    def __init__(self):
        self.funcs, self.trees, self.imports, self.by_method = {}, {}, {}, defaultdict(list)
        self.extra_edges = {}          # calls the code makes through data (a list of functions), which a reading of the calls cannot see
        for path in sorted(SRC.glob("*/*.py")):
            package = path.parent.name
            if package.endswith(".egg-info"):
                continue
            module = f"{package}.{path.stem}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self.trees[module] = tree
            self.imports[module] = self._imports(tree, package)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    self._add(Func(module, node.name, node, None))
                elif isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            fn = Func(module, f"{node.name}.{item.name}", item, node.name)
                            self._add(fn)
                            self.by_method[item.name].append(fn)

    def _add(self, fn):
        self.funcs[fn.key] = fn

    @staticmethod
    def _imports(tree, package):
        """name -> ("module", dotted) or ("name", dotted_module, original_name) for the relative imports of this package."""
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                base = f"{package}.{node.module}" if node.module else package
                for alias in node.names:
                    if node.module is None:
                        found[alias.asname or alias.name] = ("module", f"{package}.{alias.name}")
                    else:
                        found[alias.asname or alias.name] = ("name", base, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("panupgrade"):
                for alias in node.names:
                    found[alias.asname or alias.name] = ("name", node.module, alias.name)
        return found

    def calls(self, fn):
        """The first-party calls in a function, in source order, as (key, approximate)."""
        out, seen = [], set()
        nodes = sorted((n for n in ast.walk(fn.node) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
        for call in nodes:
            for key, approximate in self._resolve(call.func, fn):
                if key != fn.key and (key, approximate) not in seen:
                    seen.add((key, approximate))
                    out.append((key, approximate))
        out += [(k, False) for k in self.extra_edges.get(fn.key, []) if (k, False) not in seen]
        return out

    def _resolve(self, func, fn):
        module, known = fn.module, self.imports[fn.module]
        if isinstance(func, ast.Name):
            name = func.id
            if f"{module}.{name}" in self.funcs:
                return [(f"{module}.{name}", False)]
            if name in known and known[name][0] == "name":
                key = f"{known[name][1]}.{known[name][2]}"
                if key in self.funcs:
                    return [(key, False)]
            return []
        if not isinstance(func, ast.Attribute):
            return []
        owner, name = func.value, func.attr
        if isinstance(owner, ast.Name):
            if owner.id in known and known[owner.id][0] == "module":
                key = f"{known[owner.id][1]}.{name}"
                return [(key, False)] if key in self.funcs else []
            if owner.id in ("self", "cls") and fn.cls and f"{module}.{fn.cls}.{name}" in self.funcs:
                return [(f"{module}.{fn.cls}.{name}", False)]
        if name in _PLAIN or name.startswith("__"):
            return []
        candidates = self.by_method.get(name, [])
        if 0 < len(candidates) <= AMBIGUOUS_MAX:
            return [(c.key, len(candidates) > 1) for c in candidates]
        return []


# ---------------------------------------------------------------- the verbs

def verbs(index):
    """[(verb, help text, handler key)] read from build_parser in cli.py."""
    parser = next(f for f in ast.walk(index.trees["panupgrade.cli"]) if isinstance(f, ast.FunctionDef) and f.name == "build_parser")
    var_to_verb, found, handlers = {}, {}, {}
    for node in ast.walk(parser):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "attr", "") == "add_parser":
            var = node.targets[0].id
            verb = node.value.args[0].value
            help_text = next((k.value.value for k in node.value.keywords if k.arg == "help" and isinstance(k.value, ast.Constant)), "")
            var_to_verb[var], found[verb] = verb, help_text
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "set_defaults":
            for kw in node.keywords:
                if kw.arg == "func" and isinstance(node.func.value, ast.Name):
                    handlers[node.func.value.id] = kw.value.id
    return [(verb, found[verb], f"panupgrade.cli.{handlers[var]}") for var, verb in var_to_verb.items() if var in handlers]


def reach(index, start):
    """Every function reachable from ``start`` through the call graph (approximate edges included)."""
    seen, stack = set(), [start]
    while stack:
        key = stack.pop()
        if key in seen or key not in index.funcs:
            continue
        seen.add(key)
        stack += [k for k, _ in index.calls(index.funcs[key])]
    return seen


def node_id(key):
    return "n_" + re.sub(r"\W", "_", key)


def label(fn):
    short = fn.module.split(".", 1)[1]
    text = fn.doc.rstrip(".")[:70].replace('"', "'")
    return f"{short}.{fn.qual}" + (f"<br/><i>{text}</i>" if text else "")


def verb_chart(index, handler):
    """The handler's calls as a tree, each function drawn once (the first time it is met), a few levels deep."""
    lines, drawn, edges = ["flowchart TD"], set(), []

    def visible(fn):
        return fn.module.split(".", 1)[1] not in PRESENTATION and fn.qual not in HELPERS

    def opens(fn, depth):
        return depth == 0 or fn.module.split(".", 1)[1] in DOMAIN | {"cli"}

    def walk(key, depth):
        fn = index.funcs[key]
        lines.append(f'    {node_id(key)}["{label(fn)}"]')
        drawn.add(key)
        if depth >= MAX_DEPTH or not opens(fn, depth):
            return
        groups = {}
        for child, approximate in index.calls(fn):
            if child not in index.funcs or not visible(index.funcs[child]):
                continue
            if approximate:                                  # one node for "whichever class this turns out to be"
                groups.setdefault(child.rsplit(".", 1)[-1], []).append(index.funcs[child])
                continue
            edges.append(f"    {node_id(key)} --> {node_id(child)}")
            if child not in drawn:
                walk(child, depth + 1)
        for method, impls in groups.items():
            ident = node_id(f"{key}~{method}")
            where = " / ".join(sorted({f.qual.split('.')[0] for f in impls}))
            lines.append(f'    {ident}["{method}<br/><i>one of: {where}</i>"]')
            edges.append(f"    {node_id(key)} -.-> {ident}")
    walk(handler, 0)
    return "\n".join(lines + edges)


def verbs_doc(index):
    out = ["# What each verb calls\n",
           "Generated by `tools/flowcharts.py` from the source; do not edit by hand. A solid arrow is a call the code makes by name; a dashed "
           "arrow is a call to a method whose class is only known at run time (more than one class defines it). Presentation modules "
           "(screens, files, text) and sign-in plumbing are left out so the steps stand out; calls made through callbacks are not visible to "
           f"this reading. Each function is drawn once, at its first appearance, to a depth of {MAX_DEPTH}; only the tool's own work "
           "(planning, checks, reading firewalls, executing) is opened up.\n"]
    for verb, help_text, handler in verbs(index):
        out += [f"## `panupgrade {verb}`\n", f"{help_text}.\n" if help_text else "", f"Handler: `{handler.split('.', 2)[2]}`\n",
                "```mermaid", verb_chart(index, handler), "```\n"]
    return "\n".join(out)


# ---------------------------------------------------------------- the checks, and what feeds them

def dataclass_fields(index, module, cls):
    tree = index.trees[module]
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]


def device_checks(index):
    """[(function name, required)] from the DEVICE_CHECKS list."""
    tree = index.trees["panupgrade.checks"]
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEVICE_CHECKS":
            return [(e.elts[0].id, e.elts[1].value) for e in node.value.elts]
    raise SystemExit("DEVICE_CHECKS not found")


def attrs_used(index, key, names, seen=None):
    """Which of ``names`` a function reads as attributes, following calls to other functions of the same module."""
    seen = seen if seen is not None else set()
    fn = index.funcs[key]
    if key in seen:
        return set()
    seen.add(key)
    found = {n.attr for n in ast.walk(fn.node) if isinstance(n, ast.Attribute) and n.attr in names}
    for child, _ in index.calls(fn):
        if child in index.funcs and index.funcs[child].module == fn.module:
            found |= attrs_used(index, child, names, seen)
    return found


def collected(index):
    """From collect_facts: [(label, parse function, commands in order, Facts fields it sets)]."""
    fn = index.funcs["panupgrade.xmlops.collect_facts"]
    nested = {n.name: n for n in ast.walk(fn.node) if isinstance(n, ast.FunctionDef) and n is not fn.node}
    rows = []
    for call in sorted((n for n in ast.walk(fn.node) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "attempt"),
                       key=lambda n: n.lineno):
        label_, parse, apply = call.args[0].value, call.args[1].id, call.args[2]
        body = nested[apply.id] if isinstance(apply, ast.Name) and apply.id in nested else apply
        fields = [n.args[1].value for n in ast.walk(body) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "setattr"]
        fields += [n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)]
        parse_node = index.funcs[f"panupgrade.xmlops.{parse}"].node
        commands = [n.args[0].value for n in sorted((c for c in ast.walk(parse_node) if isinstance(c, ast.Call)
                                                      and getattr(c.func, "id", "") == "op" and c.args and isinstance(c.args[0], ast.Constant)),
                                                     key=lambda c: c.lineno)]
        rows.append((label_, parse, commands, fields))
    return rows


def checks_doc(index):
    facts = dataclass_fields(index, "panupgrade.model", "Facts")
    settings = dataclass_fields(index, "panupgrade.settings", "Settings")
    checks = device_checks(index)
    verb_list = verbs(index)
    runs = "panupgrade.checks.run_device_checks"
    reads = "panupgrade.sources.snapshot"
    verb_runs = {verb: runs in reach(index, h) for verb, _, h in verb_list}
    verb_reads = {verb: reads in reach(index, h) or "panupgrade.xmlops.collect_facts" in reach(index, h) for verb, _, h in verb_list}
    used = {name: attrs_used(index, f"panupgrade.checks.{name}", set(facts)) for name, _ in checks}
    uses_settings = {name: sorted(attrs_used(index, f"panupgrade.checks.{name}", set(settings))) for name, _ in checks}
    feed = collected(index)
    set_by = {field: label_ for label_, _, _, fields in feed for field in fields}

    out = ["# The checks, and what feeds them\n",
           "Generated by `tools/flowcharts.py` from the source; do not edit by hand.\n",
           "## Order the checks run\n",
           "`run_device_checks` runs `reachable` first; if the firewall cannot be read it stops there. Otherwise every other check runs, in "
           "this order, for every firewall in the selection. A **required** check that has no data blocks the firewall, because a "
           "collection problem must never look like success.\n",
           "```mermaid", "flowchart LR"]
    ids = [f"c{i}" for i in range(len(checks))]
    for ident, (name, required) in zip(ids, checks):
        fn = index.funcs[f"panupgrade.checks.{name}"]
        shape = ('[["', '"]]') if required else ('("', '")')
        doc = fn.doc.rstrip(".")[:60].replace('"', "'")
        out.append(f'    {ident}{shape[0]}{name}{"<br/>(required)" if required else ""}{f"<br/><i>{doc}</i>" if doc else ""}{shape[1]}')
    out += [f"    {a} --> {b}" for a, b in zip(ids, ids[1:])]
    out += ["```\n", "## Which verbs run them\n",
            "| verb | reads every selected firewall | runs the device checks |", "|---|---|---|"]
    out += [f"| `{verb}` | {'yes' if verb_reads[verb] else 'no'} | {'yes' if verb_runs[verb] else 'no'} |" for verb, _, _ in verb_list]
    out += ["", "## What each check needs\n",
            "| check | required | firewall facts it reads | settings it reads | facts nothing collects |", "|---|---|---|---|---|"]
    for name, required in checks:
        missing = sorted(f for f in used[name] if f not in set_by)
        out.append(f"| `{name}` | {'yes' if required else 'no'} | {', '.join(sorted(used[name])) or '-'} | "
                   f"{', '.join(uses_settings[name]) or '-'} | {', '.join(missing) or '-'} |")
    out += ["", "A fact in the last column is read by the check but is not set by `collect_facts`, so on a live firewall it is always empty "
            "and that check always reports \"no data\".\n",
            "## What is read from each firewall\n",
            "`collect_facts` sends these queries to **every** firewall in the selection, whichever verb is running, one after another. "
            "The last column is the checks that use what each one sets.\n",
            "| step | query sent | sets | used by checks |", "|---|---|---|---|"]
    counts = defaultdict(list)
    for label_, parse, commands, fields in feed:
        for command in commands:
            counts[command].append(label_)
        users = sorted({name for name, _ in checks for f in fields if f in used[name]})
        out.append(f"| {label_} | {'<br>'.join(f'`{c}`' for c in commands)} | {', '.join(fields)} | {', '.join(users) or '-'} |")
    repeated = {c: labels for c, labels in counts.items() if len(labels) > 1 or any(commands.count(c) > 1 for _, _, commands, _ in feed)}
    if repeated:
        out += ["", "Queries sent more than once per firewall:\n"]
        out += [f"- `{c}`: {', '.join(labels)}" for c, labels in sorted(repeated.items())]
    slug = lambda text: re.sub(r"\W", "_", text)
    out += ["", "## From the query to the verb\n",
            "Each query feeds the checks that use what it sets. Every verb marked in the table above sends all of the queries; only the "
            "verbs that run the device checks use their results.\n",
            "```mermaid", "flowchart LR", '    reads(["read every selected firewall"])', '    judged(["device checks"])']
    for label_, parse, commands, fields in feed:
        out.append(f'    q_{slug(label_)}["{label_}<br/>{commands[0] if commands else ""}"]')
        out.append(f"    reads --> q_{slug(label_)}")
    out += [f'    k_{name}("{name}")' for name, _ in checks]
    for label_, parse, commands, fields in feed:
        out += [f"    q_{slug(label_)} --> k_{name}" for name, _ in checks if any(f in used[name] for f in fields)]
    out += [f"    k_{name} --> judged" for name, _ in checks]
    for verb, _, _ in verb_list:
        if verb_reads[verb]:
            out.append(f'    v_{verb}(["panupgrade {verb}"])')
            out.append(f"    v_{verb} --> reads")
            if verb_runs[verb]:
                out += [f'    u_{verb}(["panupgrade {verb}: uses the verdicts"])', f"    judged --> u_{verb}"]
    out += ["```\n"]
    return "\n".join(out)


# ---------------------------------------------------------------- the upgrade itself: decisions, drawn from the code's own if/else

_BUILTIN_CALLS = set(dir(builtins))


def _clip(text, width=84):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _esc(text):
    return _clip(text).replace('"', "'").replace("<", "&lt;").replace(">", "&gt;").replace("#", "#35;")


def _calls_in(node):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)]


def _interesting(call):
    """A call worth a box: not a builtin or a string/dict/list method, and not a lambda body's inner plumbing."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id not in _BUILTIN_CALLS
    return isinstance(func, ast.Attribute) and func.attr not in _PLAIN


class Flow:
    """The control flow of a list of statements as a Mermaid flowchart: an ``if`` is a diamond, a call is a box, ``raise`` and ``return``
    end a path. Assignments that call nothing of interest are left out, so what remains is the decisions and the actions."""

    def __init__(self, prefix, step_call=None):
        self.prefix, self.step_call, self.count = prefix, step_call, 0
        self.lines, self.edges, self.classes = [], [], []
        self.loops = []
        self.test_names = set()     # names a decision in this chart looks at: an assignment that feeds one is drawn
        self.forced = 0             # > 0 inside a short branch, where every assignment is drawn so the branch is not empty

    def node(self, text, shape="box", cls=""):
        self.count += 1
        ident = f"{self.prefix}{self.count}"
        opened, closed = {"box": ('["', '"]'), "decision": ('{"', '"}'), "end": ('(["', '"])')}[shape]
        self.lines.append(f'    {ident}{opened}{_esc(text)}{closed}')
        if cls:
            self.classes.append(f"    class {ident} {cls}")
        return ident

    def link(self, pending, ident):
        for source, text in pending:
            self.edges.append(f'    {source} -->|{text}| {ident}' if text else f"    {source} --> {ident}")

    def block(self, statements, pending):
        """Draw the statements after the dangling edges ``pending`` ([(node, label)]); returns the edges still dangling at the end."""
        for stmt in statements:
            pending = self.statement(stmt, pending)
        return pending

    def statement(self, stmt, pending):
        if isinstance(stmt, ast.If):
            decision = self.node(ast.unparse(stmt.test), "decision")
            self.link(pending, decision)
            yes = self.branch(stmt.body, [(decision, "yes")])
            no = self.branch(stmt.orelse, [(decision, "no")]) if stmt.orelse else [(decision, "no")]
            return yes + no
        if isinstance(stmt, (ast.For, ast.While)):
            head = self.node(f"for {ast.unparse(stmt.target)} in {ast.unparse(stmt.iter)}" if isinstance(stmt, ast.For)
                             else f"while {ast.unparse(stmt.test)}", "decision")
            self.link(pending, head)
            self.loops.append((head, []))
            tail = self.block(stmt.body, [(head, "each")])
            self.link(tail, head)
            _, breaks = self.loops.pop()
            return [(head, "done")] + breaks
        if isinstance(stmt, ast.Try):
            start = self.node("try", "box")
            self.link(pending, start)
            out = self.block(stmt.body, [(start, "")])
            for handler in stmt.handlers:
                out += self.block(handler.body, [(start, f"except {ast.unparse(handler.type) if handler.type else 'anything'}")])
            return self.block(stmt.finalbody, out) if stmt.finalbody else out
        if isinstance(stmt, ast.With):
            return self.block(stmt.body, pending)
        if isinstance(stmt, ast.Raise):
            what = "raise " + (ast.unparse(stmt.exc.args[0]) if isinstance(stmt.exc, ast.Call) and stmt.exc.args else ast.unparse(stmt.exc) if stmt.exc else "")
            self.link(pending, self.node(what, "end", "refuse"))
            return []
        if isinstance(stmt, ast.Return):
            self.link(pending, self.node("return " + (ast.unparse(stmt.value) if stmt.value else ""), "end", "done"))
            return []
        if isinstance(stmt, ast.Continue) and self.loops:
            self.link(pending, self.loops[-1][0])
            return []
        if isinstance(stmt, ast.Break) and self.loops:
            self.loops[-1][1].extend(pending)
            return []
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign)):
            calls = [c for c in _calls_in(stmt) if _interesting(c)]
            if not calls and not self.state_change(stmt):
                return pending
            first = calls[0] if calls else None
            if first is not None and self.step_call and isinstance(first.func, ast.Name) and first.func.id == self.step_call:
                ident = self.node("step: " + ast.unparse(first.args[0]), "box", "step")
            else:
                text = ast.unparse(stmt)
                ident = self.node(text, "box", "api" if "backend." in text else "")
            self.link(pending, ident)
            return [(ident, "")]
        return pending

    def branch(self, statements, pending):
        """One side of a decision. A short side draws every assignment, so a side that only records an outcome is not left empty."""
        short = len(statements) <= 2
        self.forced += short
        try:
            return self.block(statements, pending)
        finally:
            self.forced -= short

    def state_change(self, stmt):
        """An assignment worth a box though it calls nothing: it records state (a subscript or attribute, or +=), feeds a decision drawn in
        this chart, or sits in a short branch."""
        if isinstance(stmt, ast.Expr):
            return False
        if self.forced or isinstance(stmt, ast.AugAssign):
            return True
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            if isinstance(target, (ast.Subscript, ast.Attribute)):
                return True
            if {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} & self.test_names:
                return True
        return False

    def render(self, body, title=""):
        for stmt in body:
            for n in ast.walk(stmt):
                if isinstance(n, (ast.If, ast.While)):
                    self.test_names |= {m.id for m in ast.walk(n.test) if isinstance(m, ast.Name)}
        start = self.node(title or "start", "end", "entry")
        self.block(body, [(start, "")])
        return "\n".join(["flowchart TD"] + self.lines + self.edges + [
            "    classDef api stroke:#2f6fed,stroke-width:3px", "    classDef refuse stroke:#d64545,stroke-width:3px",
            "    classDef done stroke:#2e9e5b,stroke-width:3px", "    classDef step stroke:#e0a020,stroke-width:3px",
            "    classDef entry stroke-width:3px"] + self.classes)


def find_function(index, key, nested=None):
    node = index.funcs[key].node
    if nested is None:
        return node
    return next(n for n in ast.walk(node) if isinstance(n, ast.FunctionDef) and n.name == nested)


def action_branches(do_node):
    """[(test text, body, line)] for the if/elif chain on ``action`` in ``_do``, and the final else."""
    chain = next(s for s in do_node.body if isinstance(s, ast.If) and "action" in ast.unparse(s.test) and "backup_verified" not in ast.unparse(s.test))
    out, node = [], chain
    while True:
        out.append((ast.unparse(node.test), node.body, node.lineno))
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
        else:
            out.append(("anything else", node.orelse, node.orelse[0].lineno if node.orelse else node.lineno))
            return out


def module_constants(index, module):
    """name -> source text of the module-level assignments (the commands kept as constants)."""
    found = {}
    for node in index.trees[module].body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = node.value
    return found


def backend_commands(index, method):
    """What LiveBackend.<method> sends to a firewall and what it then waits for: [(kind, text)] in the order the code does it."""
    key = f"panupgrade_live.backend.LiveBackend.{method}"
    if key not in index.funcs:
        return []
    constants = module_constants(index, "panupgrade_live.backend")
    out = []

    defaults = settings_defaults(index)

    def resolve(arg):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        shown = ast.unparse(arg)
        named = re.fullmatch(r"self\.settings\.(\w+)", shown)
        if named and named.group(1) in defaults:                  # a timeout: say what it is, not where it is read from
            return f"{named.group(1)} ({defaults[named.group(1)]} s by default)"
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and f"panupgrade_live.backend.{arg.func.id}" in index.funcs:
            helper = index.funcs[f"panupgrade_live.backend.{arg.func.id}"].node
            built = next((s.value for s in helper.body if isinstance(s, ast.Return)), None)
            if built is not None:
                return ast.unparse(built)
        if isinstance(arg, ast.Name) and arg.id in constants:
            return resolve(constants[arg.id]) if isinstance(constants[arg.id], ast.Constant) else ast.unparse(constants[arg.id])
        if isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name) and isinstance(constants.get(arg.value.id), ast.Dict):
            table = constants[arg.value.id]
            for k, v in zip(table.keys, table.values):
                if isinstance(k, ast.Constant) and isinstance(arg.slice, ast.Constant) and k.value == arg.slice.value:
                    return resolve(v)
        return ast.unparse(arg)
    def duration(arg):
        text = resolve(arg)
        if text.isdigit():
            return f"{text} s"
        return f"{text} given by the caller" if text.isidentifier() else text
    for call in sorted(_calls_in(index.funcs[key].node), key=lambda c: (c.lineno, c.col_offset)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if name in ("_send", "op_xml", "op") and call.args:
            arg = call.args[0] if name != "_send" else call.args[0]
            out.append(("sends", resolve(arg)))
        elif name == "_wait_until" and len(call.args) >= 4:
            out.append(("waits", f"up to {duration(call.args[2])} for {resolve(call.args[3])}"))
        elif name == "_wait_job" and len(call.args) >= 4:
            out.append(("waits", f"up to {duration(call.args[2])} for the job: {resolve(call.args[3])}"))
        elif name == "collect_facts":
            out.append(("reads", "the firewall's state (the queries listed in checks.md)"))
    return out


def settings_defaults(index):
    tree = index.trees["panupgrade.settings"]
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    return {n.target.id: ast.unparse(n.value) for n in cls.body if isinstance(n, ast.AnnAssign) and n.value is not None}


def steps_by_role():
    """The step lists planner._steps really produces for a model firewall of each kind (run, not described)."""
    sys.path.insert(0, str(SRC))
    from panupgrade import planner, settings as settings_mod
    settings = settings_mod.Settings()
    path = [{"download": True, "install": True, "version": "TARGET"}]
    results = [{"check": "upgrade_path", "evidence": {"steps": path}, "status": "pass"}]

    def firewall(serial, mode, state, peer, preemption=False):
        return {"serial": serial, "hostname": serial, "ha_mode": mode, "ha_state": state, "peer_serial": peer, "preemption": preemption,
                "results": results, "verdict": "ready", "current_version": "CURRENT"}
    pair = {"A": firewall("A", "active-passive", "active", "P"), "P": firewall("P", "active-passive", "passive", "A")}
    cases = [("standalone", firewall("S", "none", "active", None), None),
             ("HA passive (its active peer is in the run)", pair["P"], pair),
             ("HA active, passive peer in the run", pair["A"], pair),
             ("HA active, peer not in the run", pair["A"], {"A": pair["A"]}),
             ("HA active, preemption on, peer in the run", firewall("A", "active-passive", "active", "P", True), pair)]
    return [(name, [s["action"] for s in planner._steps(dev, settings, devices)[0]]) for name, dev, devices in cases]


HA_WATCH = {"ha_config_sync", "ha_session_sync", "session_count", "_band_ok", "failover_band", "post_upgrade_band"}
STAGE_ORDER = ["checks", "planner", "prepare", "executor", "fleetreport", "report"]


def ha_decisions(index):
    """Every decision in the code that looks at HA synchronisation or the session count: (module, function, line, condition, verbs that reach it)."""
    handlers = {verb: handler for verb, _, handler in verbs(index) if verb in ("report", "plan", "prepare", "upgrade")}
    reaches = {verb: reach(index, handler) for verb, handler in handlers.items()}
    rows = []
    for fn in index.funcs.values():
        module = fn.module.split(".", 1)[1]
        if module in ("shell", "tui", "console", "simulator", "model", "xmlops", "sources", "settings"):
            continue
        for n in ast.walk(fn.node):
            if isinstance(n, (ast.If, ast.IfExp, ast.While)):
                names = {m.attr for m in ast.walk(n.test) if isinstance(m, ast.Attribute)} | {m.id for m in ast.walk(n.test) if isinstance(m, ast.Name)}
                names |= {m.value for m in ast.walk(n.test) if isinstance(m, ast.Constant) and isinstance(m.value, str)}
                if names & HA_WATCH:
                    rows.append((module, fn, n.lineno, ast.unparse(n.test),
                                 [v for v in handlers if fn.key in reaches[v]]))
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: (STAGE_ORDER.index(r[0]) if r[0] in STAGE_ORDER else 99, r[2])):
        key = (row[1].key, row[3])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def upgrade_doc(index):
    do_node = find_function(index, "panupgrade.executor._do")
    defaults = settings_defaults(index)
    used_settings, used_backend, used_helpers = set(), [], {}
    executor_helpers = {n: index.funcs[k] for k, n in ((k, k.rsplit(".", 1)[1]) for k in index.funcs if k.startswith("panupgrade.executor.") and k.count(".") == 2)}

    def note(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "settings" and n.attr in defaults:
                used_settings.add(n.attr)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "backend" and n.attr not in used_backend:
                used_backend.append(n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in executor_helpers:
                used_helpers[n.func.id] = executor_helpers[n.func.id]

    def chart(prefix, body, title, step_call=None):
        for stmt in body:
            note(stmt)
        return ["```mermaid", Flow(prefix, step_call).render(body, title), "```\n"]

    out = ["# The upgrade, decision by decision\n",
           "Generated by `tools/flowcharts.py` from the source; do not edit by hand. Each chart is the function's own `if`/`else`, loops "
           "and `try`, drawn as the code is written: **diamonds are decisions** (the condition is the code's own text), boxes are actions "
           "(a blue outline is a call on the backend, which is what touches a firewall), a red outline is a refusal (the step fails and the "
           "firewall is not touched further), a green outline is a normal end. Assignments that call nothing are left out. Function "
           "names carry their file and line so the code can be read beside the chart.\n",
           "How it fits together: `plan` gives every firewall a list of steps (section 1). `upgrade` runs each unit, an HA pair or a "
           "standalone firewall, in the order the plan gave (section 2); units run in parallel up to `max_parallel`, the members of a pair "
           "one after the other, passive first. Each step of the list is carried out by `_do` (section 3), one chart per step.\n",
           "## 1. Which steps a firewall is given\n",
           f"`planner._steps` ([planner.py:{index.funcs['panupgrade.planner._steps'].node.lineno}]). The same function, run for a model firewall "
           "of each kind with default settings (`TARGET` is a version, `CURRENT` the one it runs now):\n"]
    for name, steps in steps_by_role():
        out.append(f"- **{name}**: " + " → ".join(f"`{s}`" for s in steps))
    out += [""] + chart("s", find_function(index, "panupgrade.planner._steps").body, "planner._steps", step_call="add")
    out += ["## 2. How a unit and a firewall are run\n",
            f"`executor.run`, for each unit ([executor.py:{index.funcs['panupgrade.executor.run'].node.lineno}]). A member is run only when "
            "the one before it in the unit finished; a firewall that failed earlier in the journal is not retried.\n"]
    out += chart("u", find_function(index, "panupgrade.executor.run", "do_unit").body, "executor.run: do_unit")
    out += [f"`executor._run_device` ([executor.py:{index.funcs['panupgrade.executor._run_device'].node.lineno}]): preflight, then each step in "
            "order. A step already marked done in the journal is skipped, and one that was started and never finished is never repeated: "
            "it needs a person to look at the firewall.\n"]
    out += chart("r", find_function(index, "panupgrade.executor._run_device").body, "executor._run_device")
    out += [f"`executor._preflight` ([executor.py:{index.funcs['panupgrade.executor._preflight'].node.lineno}]): the plan is checked against "
            "the firewall again just before it is touched.\n"]
    out += chart("p", find_function(index, "panupgrade.executor._preflight").body, "executor._preflight")
    out += [f"`executor._step` ([executor.py:{index.funcs['panupgrade.executor._step'].node.lineno}]) wraps every step: write `started`, run it, "
            "write `ok` or `failed`.\n"]
    out += chart("w", find_function(index, "panupgrade.executor._step").body, "executor._step")
    out += ["## 3. How each step is carried out\n",
            f"`executor._do` ([executor.py:{do_node.lineno}]). Before any step that installs or reboots, a verified backup must be in the "
            "journal:\n"]
    out += chart("g", [s for s in do_node.body if isinstance(s, ast.If)][0:1], "executor._do: guard")
    for number, (test, body, line) in enumerate(action_branches(do_node), 1):
        step = test.replace("action == ", "").replace("action.startswith(", "starts with ").rstrip(")") if test != "anything else" else test
        out += [f"### {step}\n", f"executor.py:{line}\n"]
        out += chart(f"d{number}_", body, f"step: {step}")
    out += ["## Where the code decides about HA synchronisation and the session count\n",
            "Found by reading every decision in the code that looks at the config-sync or session-sync state, the session count, or "
            "the two bands. The last column is the verbs that reach the function (by name; calls through callbacks are not seen). "
            "Earlier rows decide before anything is touched; the `executor` rows decide during the upgrade.\n",
            "| module | function (line) | the condition, as written | verbs |", "|---|---|---|---|"]
    for module, fn, line, condition, reached in ha_decisions(index):
        out.append(f"| {module} | `{fn.qual}` ({module}.py:{line}) | `{_clip(condition, 120).replace('|', '/')}` | {', '.join(reached) or '-'} |")
    out += ["", "## Helpers the charts call\n", "| function | what it does |", "|---|---|"]
    for name, fn in sorted(used_helpers.items()):
        if name in ("_do", "_step", "_run_device", "_preflight"):
            continue
        body = [s for s in fn.node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        text = ast.unparse(body[0]) if len(body) == 1 else (fn.doc or "see the source")
        out.append(f"| `{name}` (executor.py:{fn.node.lineno}) | {_clip(text, 150).replace('|', '/')} |")
    out += ["", "## Settings the charts read\n", "| setting | default |", "|---|---|"]
    out += [f"| `{name}` | `{defaults[name]}` |" for name in sorted(used_settings)]
    out += ["", "## What each backend call does on a live firewall\n",
            "From `LiveBackend` ([backend.py](../../src/panupgrade_live/backend.py)). The simulator (`SimBackend`) models the same calls "
            "without sending anything. Commands are Panorama API operations, run against the firewall.\n",
            "| call | sends | then waits |", "|---|---|---|"]
    for method in used_backend:
        commands = backend_commands(index, method)
        if not commands:
            continue
        sends = "<br>".join(f"`{t}`" for k, t in commands if k in ("sends", "reads")) or "-"
        waits = "<br>".join(t for k, t in commands if k == "waits") or "-"
        out.append(f"| `backend.{method}` | {sends.replace('|', '/')} | {waits} |")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- output

def build_index():
    index = Index()
    index.extra_edges["panupgrade.checks.run_device_checks"] = [f"panupgrade.checks.{name}" for name, _ in device_checks(index)]
    return index


def documents():
    index = build_index()
    return {"verbs.md": verbs_doc(index), "checks.md": checks_doc(index), "upgrade.md": upgrade_doc(index)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the files in docs/flow are not what the source would produce")
    args = parser.parse_args(argv)
    stale = []
    for name, text in documents().items():
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("out of date: " + ", ".join(stale) + " (run python tools/flowcharts.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
