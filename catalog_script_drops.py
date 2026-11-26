"""Recover corpse grants from creature death scripts with authored bindings."""

from collections import defaultdict
import re

from catalog_item_scripts import Bindings, CALL, CREATORS, function, script_methods, source_views
from catalog_sources import ROOT, SCRIPTS, arguments, source


CORPSE_CALLBACKS = {"OnDeath", "aiCorpsePrepared", "OnCorpsePrepared"}
ATTACH = re.compile(r'\battachScript\s*\(\s*(\w+)\s*,\s*"([\w.]+)"')
ASSIGN = re.compile(r"\b(\w+)\s*=\s*(?:create\s*\.\s*(?:object|createNpc)|createObject)\s*\(")
LOCAL_CALL = re.compile(r"(?<![\w.])(\w+)\s*\(")


def _spawn_attachments(spawn_index, files, eligible):
    bindings = defaultdict(dict)
    if not spawn_index:
        return bindings
    for calls in spawn_index.scripts.by_script.values():
        eligible_calls = [call for call in calls if not call.development and call.target in eligible and call.mode == "creature"]
        if not eligible_calls:
            continue
        path = ROOT / eligible_calls[0].path["path"]
        clean, code = source_views(files.text(path))
        lines, grammar = clean.splitlines(), code.splitlines()
        for call in eligible_calls:
            index = call.path["line"] - 1
            if not (assignment := ASSIGN.search(grammar[index])):
                continue
            name = assignment[1]
            for offset in range(index, min(index + 12, len(lines))):
                line = grammar[offset]
                if offset > index and (line.lstrip().startswith("}") or re.search(rf"\b{re.escape(name)}\s*=(?!=)", line)):
                    break
                for match in ATTACH.finditer(lines[offset]):
                    if line[match.start():match.start() + 12].lower().startswith("attachscript") and match[1] == name:
                        bindings[call.target].setdefault(match[2], source(path, offset + 1))
    return bindings


def _reachable(ends, code):
    available = {method[1] for method, _ in ends}
    graph = defaultdict(set)
    for method, end in ends:
        graph[method[1]].update(match[1] for match in LOCAL_CALL.finditer(code[method.end():end]) if match[1] in available)
    seen = set()
    pending = list(available & CORPSE_CALLBACKS)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(graph[name] - seen)
    return seen


def _corpse_item_acquisitions(proofs, path, files):
    clean, code = source_views(files.text(path))
    methods, fields = script_methods(clean, code)
    reachable = _reachable(methods, code)
    for method, end in methods:
        if method[1] not in reachable:
            continue
        start = method.end()
        body = code[start:end]
        bindings = Bindings(clean[method.start():end], code[method.start():end], fields)
        for call in CALL.finditer(body):
            line = code.count("\n", 0, start + call.start()) + 1
            if line not in proofs:
                continue
            module, name = call.groups()
            key = f"{module}.{name}" if module else name
            if key not in CREATORS:
                continue
            _, behavior = CREATORS[key]
            values = arguments(clean, start + call.end() - 1)
            pos = 0 if behavior in {"loot_item", "loot_table"} else 1
            if pos >= len(values):
                continue
            expression = values[pos]
            owner, assignments, _, _ = bindings.binding(expression, start - method.start() + call.start())
            if owner and len(assignments) == 1:
                expression = assignments[0][1]
            _, getter, arguments_ = function(expression)
            if getter == "getInventoryContainer" and arguments_ and arguments_[0].lower() in {
                "self", "corpse", "corpseid", "objcorpse", "getself()",
            }:
                yield from ((item, source(path, line)) for item in proofs[line])


def scripted_drops(files, items, npcs, spawn_index):
    """Return proven drops whose encounter gate cannot be folded into a base kill."""
    sources = defaultdict(lambda: defaultdict(set))
    paths = {}
    for item, record in items.records.items():
        for acquisition in record["acquisitions"]:
            if acquisition["kind"] == "script" and not acquisition.get("development"):
                script, proof = acquisition["script"], acquisition["source"]
                sources[script][proof["line"]].add(item)
                paths[script] = ROOT / proof["path"]
    assignments = _spawn_attachments(spawn_index, files, npcs)
    reverse = defaultdict(dict)
    for npc, profile in npcs.items():
        for script in profile["scripts"]:
            reverse[script][npc] = next(iter(profile["sources"]), None)
        for script, proof in assignments[npc].items():
            reverse[script][npc] = proof
    for script, candidates in sources.items():
        if script not in reverse:
            continue
        if script == "city.bestine.tusken_spawner":
            continue
        path = paths[script]
        for item, proof in sorted({(item, origin["line"]) for item, origin in _corpse_item_acquisitions(candidates, path, files)}):
            for npc, attachment in reverse[script].items():
                yield npc, item, script, None, source(path, proof), attachment, "Creature death script"
    # This encounter rolls one integer for three disjoint item bands. Entry
    # requires both the attached raid script and an eligible quest looter.
    script = "city.bestine.tusken_spawner"
    path = SCRIPTS / "city/bestine/tusken_spawner.script"
    if not files.exists(path) or script not in reverse:
        return
    text = files.text(path)
    if not re.search(r"rand\(0,\s*7000\)", text) or '"bestine.tuskenquest"' not in text:
        return
    for key, start, stop in (("CARVED", 0, 70), ("SMOOTH", 71, 300), ("BATON", 301, 500), ("HEAD", 0, 7000)):
        if not (match := re.search(rf'const string {key}\s*=\s*"([^"]+)"', text)):
            continue
        if key != "HEAD" and not re.search(rf"chance\s*>=\s*{start}.*?chance\s*<=\s*{stop}", text):
            continue
        proof = source(path, text.count("\n", 0, match.start()) + 1)
        for npc, attachment in reverse[script].items():
            if key != "HEAD" or npc == "tusken_executioner":
                yield npc, items.resolve_item(match[1]) or match[1], script, (stop - start + 1) / 7001, proof, attachment, "Bestine raid with an eligible quest looter"
