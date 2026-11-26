"""Trace literal quest calls and authored bindings in the effective script corpus."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re

from .manifest_sources import (
    PLANETS, QUEST_ID, SERVER, SCRIPTS, TEST_ID, add_unique, edge, place, planet, quest_id, requirement,
    reward, source,
)


QUOTED = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
COMMENT = re.compile(QUOTED + r"|//[^\n]*|/\*.*?\*/", re.DOTALL)
TOKEN = re.compile(QUOTED + r"|[(){},\[\]]")
METHOD = re.compile(
    r"^[ \t]*(?:(?:public|protected|private|static|final|synchronized)\s+)*"
    r"(?:[\w.<>\[\]]+)[ \t]+(\w+)\s*\([^(){};]*\)\s*"
    r"(?:throws\s+[\w., \t\n]+\s*)?\{",
    re.MULTILINE,
)
CALL = re.compile(
    r"\b(?:(groundquests|space_quest|quests)\s*\.\s*)?"
    r"(requestGrantQuest|grantQuestNoAcceptUI|grantGroupQuest|grantQuest|"
    r"questActivateQuest|requestActivateQuest|activate|giveReward)\s*\("
)
CONST = re.compile(r'\b(?:String|string|int)\s+(\w+)\s*=\s*([^;\n]+);')
QUEST_CRC = re.compile(r"(?:(?:groundquests)\s*\.\s*)?(?:questGetQuestId|getQuestIdFromString)\s*\((.*)\)")
SCRIPT_ID = re.compile(r"(?<![\w.])(?:\w+\.)+\w+(?![\w.])")
SPAWN_LOCATION = re.compile(r"\blocation\s+(\w+)\s*=\s*new\s+location\s*\(")
SPAWN_CALL = re.compile(r"\b(\w+)\s*=\s*create\s*\.\s*object\s*\(")
ATTACH_CALL = re.compile(r'\battachScript\s*\(\s*(\w+)\s*,\s*"([\w.]+)"\s*\)')
AREA_SPAWNS = re.compile(r"(?:^|\|)strSpawns\|4\|([^|]+)")
GUARD = re.compile(r"\bif\s*\(")
BRANCH_HANDLER = re.compile(r"_handleBranch(\d+)$")
BRANCH_ASSIGNMENT = re.compile(r'\butils\.setScriptVar\s*\([^,]+,\s*"conversation\.[\w.]+\.branchId"\s*,\s*(\d+)\s*\)')
RESPONSE_OFFER = re.compile(r'\bresponses\s*\[[^\]]+\]\s*=\s*new\s+string_id\s*\(\s*c_stringFile\s*,\s*"([^"]+)"\s*\)')
RESPONSE_TEST = re.compile(r'\bresponse\s*(?:==\s*"([^"]+)"|\.equals\s*\(\s*"([^"]+)"\s*\))')
ELIGIBILITY_CALL = re.compile(
    r"\b(hasCompletedQuest|questHasCompletedQuest|questIsQuestComplete|hasWonQuest|"
    r"hasReceivedReward|hasSkill|hasProfession|isProfession|getLevel|getFactionStanding|"
    r"isImperial|isRebel|isImperialorImperialHelper|isRebelorRebelHelper|getSpaceTrack|"
    r"hasObjVar|isTaskActive|isQuestActive|pvpGetAlignedFaction)\s*\("
)


def balanced_end(text: str, start: int) -> int:
    left = text[start]
    right = {"(": ")", "{": "}", "[": "]"}[left]
    depth = 0
    for token in TOKEN.finditer(text, start):
        if token[0] == left:
            depth += 1
        elif token[0] == right:
            depth -= 1
            if not depth:
                return token.start()
    return len(text)


def arguments(text: str, start: int) -> list[str]:
    end = balanced_end(text, start)
    depth = 0
    mark = start + 1
    result = []
    for token in TOKEN.finditer(text, mark, end):
        if token[0] in ("(", "[", "{"):
            depth += 1
        elif token[0] in (")", "]", "}"):
            depth -= 1
        elif token[0] == "," and depth == 0:
            result.append(text[mark:token.start()].strip())
            mark = token.end()
    result.append(text[mark:end].strip())
    return result


def assignments(text):
    result = defaultdict(set)
    for name, value in CONST.findall(text):
        result[name].add(value.strip())
    return {name: next(iter(options)) for name, options in result.items() if len(options) == 1}


def literal(expression, symbols):
    for _ in range(5):
        if re.fullmatch(r'"[^"\\]*"', expression):
            return expression[1:-1]
        if re.fullmatch(r"-?\d+", expression):
            return int(expression)
        if re.fullmatch(r"-?\d+\.\d+[fF]?", expression):
            return float(expression.rstrip("fF"))
        crc = QUEST_CRC.fullmatch(expression)
        if crc:
            expression = crc[1].strip()
            continue
        expression = symbols.get(expression, "")
        if not expression:
            break
    return None


@dataclass
class Method:
    body: str
    offset: int
    line: int


class Script:
    def __init__(self, path: Path, text: str):
        self.path = path
        self.text = COMMENT.sub(
            lambda match: match[0] if match[0][0] in "\"'" else re.sub(r"[^\n]", " ", match[0]),
            text,
        )
        self.line_ends = [match.start() for match in re.finditer("\n", self.text)]
        self.methods = {}
        start = 0
        for match in METHOD.finditer(self.text):
            if match.start() < start or match[1] in ("if", "for", "while", "switch", "catch"):
                continue
            brace = match.end() - 1
            start = balanced_end(self.text, brace)
            self.methods[match[1]] = Method(self.text[brace + 1:start], brace + 1, self.line(match.start()))
        first = min((method.offset for method in self.methods.values()), default=len(self.text))
        self.constants = assignments(self.text[:first])
        self._offers = None

    def line(self, offset):
        return bisect_right(self.line_ends, offset) + 1

    def evidence(self, method, offset):
        return source(self.path, self.line(method.offset + offset))

    def response_offers(self):
        if self._offers is None:
            self._offers = defaultdict(list)
            for name, method in self.methods.items():
                for offer in RESPONSE_OFFER.finditer(method.body):
                    branch = BRANCH_ASSIGNMENT.search(method.body, offer.end())
                    if branch and not re.search(r"\breturn\b", method.body[offer.end():branch.start()]):
                        self._offers[(branch[1], offer[1])].append((name, offer.start()))
        return self._offers


def script_paths(files):
    # Same selection as the default script compiler. Read only the preserved
    # Java where it exists and the legacy source otherwise.
    selected = {}
    for path in files.paths(SCRIPTS, "*", recursive=True):
        if path.suffix not in (".java", ".script", ".scriptlib") or ".deps" in path.parts:
            continue
        ident = ".".join(path.relative_to(SCRIPTS).with_suffix("").parts)
        if ident not in selected or path.suffix == ".java":
            selected[ident] = path
    return selected


def bindings(scripts, strings, files):
    sites = defaultdict(list)
    mobs = {
        row.get("creatureName"): row for row in files.tab(SERVER / "mob/creatures.tab")
        if row.get("creatureName")
    }
    locations = defaultdict(list)
    for path in files.paths(SERVER / "spawning/poi_spawner", "*.tab"):
        for row in files.tab(path):
            npc_id = row.get("TYPE")
            scene = row.get("PLANET") or path.stem
            if not npc_id:
                continue
            position = [row.get("LOC_" + axis) for axis in "XYZ"]
            evidence = row.source
            npc = strings.npc(npc_id)
            details = {"npc": npc}
            if all(position):
                details["position"] = position
            if not row.get("BUILDING") or row.get("BUILDING") == "0":
                details["coordinate_frame"] = "world"
            else:
                details["container"] = row.get("BUILDING")
            site = place("npc", evidence, scene, **details)
            locations[npc_id].append(site)
            declared = row.get("SCRIPTS")
            if npc_id in mobs:
                declared += " " + mobs[npc_id].get("scripts")
            for name in set(SCRIPT_ID.findall(declared)) & scripts.keys():
                sites[name].append(site)

    anchors = defaultdict(list)
    spawn_types = {}
    for path in files.paths(SERVER / "buildout", "*/*.tab"):
        for row in files.tab(path):
            bound = set(SCRIPT_ID.findall(row.get("scripts")))
            if "space.content_tools.npc_spawner" in bound and row.get("objid"):
                anchors[row.get("objid")].append((path.parent.name, row.source))
            area_spawns = AREA_SPAWNS.search(row.get("objvars"))
            if area_spawns and "systems.spawning.spawner_area" in bound:
                spawn_type = area_spawns[1]
                if spawn_type not in spawn_types:
                    table = SERVER / "spawning/ground_spawning/types" / f"{spawn_type}.tab"
                    spawn_types[spawn_type] = ([(item.get("strItem"), item.source) for item in files.tab(table)]
                                               if QUEST_ID.fullmatch(spawn_type) and files.exists(table)
                                               else [(spawn_type, None)])
                for npc_id, type_source in spawn_types[spawn_type]:
                    if npc_id in mobs:
                        site = place("npc", row.source, path.parent.name, npc=strings.npc(npc_id),
                                     spawner_object_id=row.get("objid") or None, spawn_type_source=type_source)
                        add_unique(locations[npc_id], site)
                        for name in set(SCRIPT_ID.findall(mobs[npc_id].get("scripts"))) & scripts.keys():
                            add_unique(sites[name], site)
            bound &= scripts.keys()
            if not bound:
                continue
            template = row.get("server_template_crc") or row.get("server_template")
            for name in bound:
                site = place("placed_object", row.source, path.parent.name, template=template or None)
                if template.startswith(("object/creature/", "object/mobile/")):
                    site["kind"] = "npc"
                    site["npc"] = strings.npc(Path(template).stem)
                sites[name].append(site)
    bind_space_npcs(sites, anchors, scripts, strings, files)
    bind_scripted_npcs(sites, locations, mobs, scripts, strings, files)

    for npc_id, row in mobs.items():
        if npc_id in locations:
            continue
        for name in set(SCRIPT_ID.findall(row.get("scripts"))) & scripts.keys():
            details = {"npc": strings.npc(npc_id)}
            where = row.get("where").lower()
            if where:
                details["mob_where_hint"] = where
            if planet(where):
                details["planet_hint"] = planet(where)
                details["planet_hint_basis"] = "mob_where"
            sites[name].append(place("npc", row.source, **details))

    for path in files.paths(SERVER / "space_trainer_spawner", "*.tab"):
        for row in files.tab(path):
            ident = "conversation." + row.get("convoScript")
            if ident in scripts:
                name = row.get("npcName")
                npc = {"id": row.get("convoScript"), **strings.label("@" + name if ":" in name else name)}
                sites[ident].append(place("npc", row.source, npc=npc, spawner=path.stem))
    return sites, locations


def bind_space_npcs(sites, anchors, scripts, strings, files):
    for row in files.tab(SERVER / "space_content/npc_spawners.tab"):
        template = row.get("strTemplate")
        if not template.startswith("object/"):
            continue
        npc_id = row.get("sidName")
        label = strings.label(f"@npc_spawner_n:{npc_id}")
        if not label["text"] and row.get("oldStrName_now_in_npc_spawner_n"):
            label["text"] = row.get("oldStrName_now_in_npc_spawner_n")
            label["basis"] = "authored_previous_name"
        npc = {"id": npc_id, **label}
        anchor_id = row.get("strObjId")
        anchor_rows = anchors.get(anchor_id) or [(None, None)]
        declared = {row.get(f"strScript{n}") for n in range(1, 5)}
        for name in declared & scripts.keys():
            for scene, anchor in anchor_rows:
                site = place("npc", row.source, scene, npc=npc, anchor_object_id=anchor_id,
                             template=template, placement_source=anchor)
                if row.get("strComments"):
                    site["placement_note"] = row.get("strComments").strip('"')
                sites[name].append(site)


def bind_scripted_npcs(sites, locations, mobs, scripts, strings, files):
    for controller, placements in sorted(sites.items()):
        scenes = {placement["scene"] for placement in placements if placement["scene"]}
        if not scenes:
            continue
        path = scripts[controller]
        text = files.text(path)
        if not SPAWN_LOCATION.search(text) or not SPAWN_CALL.search(text):
            continue
        parsed = Script(path, text)
        for method in parsed.methods.values():
            locs = defaultdict(list)
            for location in SPAWN_LOCATION.finditer(method.body):
                locs[location[1]].append(arguments(method.body, location.end() - 1))
            attachments = defaultdict(set)
            for var, attached in ATTACH_CALL.findall(method.body):
                attachments[var].add(attached)
            for spawn in SPAWN_CALL.finditer(method.body):
                args = arguments(method.body, spawn.end() - 1)
                if len(args) < 2 or len(locs[args[1]]) != 1:
                    continue
                loc = locs[args[1]][0]
                if len(loc) < 4:
                    continue
                scene = literal(loc[3], parsed.constants)
                npc_id = literal(args[0], parsed.constants)
                if scene not in scenes or npc_id not in mobs:
                    continue
                evidence = parsed.evidence(method, spawn.start())
                details = {"npc": strings.npc(npc_id), "controller": controller,
                           "placement_source": next(site["source"] for site in placements if site["scene"] == scene)}
                if len(loc) > 4:
                    details["container_expression"] = loc[4]
                site = place("npc", evidence, scene, **details)
                declared = set(SCRIPT_ID.findall(mobs[npc_id].get("scripts"))) | attachments[spawn[1]]
                for name in declared & scripts.keys():
                    add_unique(sites[name], site)
                add_unique(locations[npc_id], site)


def concrete_quest(args, module, name, symbols):
    decoded = [literal(arg, symbols) for arg in args]
    if module == "quests" and name == "activate" and decoded and isinstance(decoded[0], str):
        return "legacy/journal/" + decoded[0]
    if module == "space_quest" and len(decoded) >= 3:
        quest_type, quest_name = decoded[1:3]
        if isinstance(quest_type, str) and isinstance(quest_name, str):
            return f"spacequest/{quest_type}/{quest_name}"
    if module == "groundquests" or name in ("questActivateQuest", "requestActivateQuest"):
        if len(decoded) >= 2 and isinstance(decoded[1], str):
            return quest_id(decoded[1])
        if decoded and isinstance(decoded[0], str):
            return quest_id(decoded[0])
    return None


def guards(body, offset):
    result = []
    for match in GUARD.finditer(body, 0, offset):
        start = match.end() - 1
        end = balanced_end(body, start)
        if end >= offset:
            continue
        tail = re.match(r"\s*", body[end + 1:]).end() + end + 1
        if tail >= len(body):
            continue
        stop = balanced_end(body, tail) if body[tail] == "{" else body.find(";", tail)
        if tail <= offset < stop:
            result.append((start, " ".join(body[start + 1:end].split())))
    return result


def pure_predicate(body):
    returned = re.fullmatch(r"\s*return\s+([^;]+);\s*", body)
    if returned:
        return returned[1]
    branch = re.match(r"\s*if\s*\(", body)
    if branch:
        start = branch.end() - 1
        end = balanced_end(body, start)
        outcome = body[end + 1:].strip()
        if re.fullmatch(r"(?:return\s+true;|\{\s*return\s+true;\s*\})\s*"
                        r"(?:else\s*)?(?:return\s+false;|\{\s*return\s+false;\s*\})", outcome):
            return body[start + 1:end]
    return None


def conditions(parsed, name, offset):
    method = parsed.methods[name]
    for start, expression in guards(method.body, offset):
        if ELIGIBILITY_CALL.search(expression):
            yield expression, parsed.evidence(method, start)
        for match in re.finditer(r"(?<![!.\w])(\w+_condition_\w+)\s*\(\s*player\s*,\s*npc\s*\)", expression):
            called = parsed.methods.get(match[1])
            if called and ELIGIBILITY_CALL.search(called.body):
                predicate = pure_predicate(called.body)
                if predicate and re.fullmatch(r"\(*\s*" + re.escape(match[0]) + r"\s*\)*", expression):
                    yield " ".join(predicate.split()), source(parsed.path, called.line)
                else:
                    yield expression, source(parsed.path, called.line)


def dialogue_conditions(parsed, name, offset):
    # An action runs in a generated response handler. A uniquely authored
    # offer for that branch/response establishes the preceding route gates.
    # Stop at joins, loops, or scripts that construct responses dynamically.
    if "_action_" in name:
        invocation = re.compile(r"\b" + re.escape(name) + r"\s*\(\s*player\s*,\s*npc\s*\)")
        for caller, method in parsed.methods.items():
            if caller != name:
                for call in invocation.finditer(method.body):
                    yield from response_conditions(parsed, caller, call.start())
    else:
        yield from response_conditions(parsed, name, offset, include_local=False)


def response_conditions(parsed, name, offset, include_local=True):
    visited = set()
    for _ in range(6):
        if (name, offset) in visited:
            break
        visited.add((name, offset))
        if include_local:
            yield from conditions(parsed, name, offset)
        include_local = True
        branch = BRANCH_HANDLER.search(name)
        if not branch:
            break
        keys = [match[1] or match[2]
                for _, expression in guards(parsed.methods[name].body, offset)
                if (match := RESPONSE_TEST.fullmatch(expression))]
        offers = parsed.response_offers().get((branch[1], keys[0])) if len(keys) == 1 else None
        if not offers or len(offers) != 1:
            break
        name, offset = offers[0]


def required_completion(expression, symbols):
    # A top-level AND of literal positive predicates establishes a route gate.
    # Preserve more complex Boolean conditions verbatim in the start evidence.
    if "||" in expression:
        return
    for part in expression.split("&&"):
        part = part.strip().lstrip("(").strip()
        match = re.match(r"(?:(groundquests|space_quest)\s*\.\s*)?"
                         r"(hasCompletedQuest|questHasCompletedQuest|questIsQuestComplete|hasReceivedReward|hasWonQuest)\s*\(", part)
        if match and not part[balanced_end(part, match.end() - 1) + 1:].strip().strip(")"):
            target = concrete_quest(arguments(part, match.end() - 1), match[1] or "groundquests",
                                    match[2], symbols)
            if target:
                predicate = "space_reward_received" if match[2] == "hasReceivedReward" else "quest_completed"
                yield target, predicate


def add_script_sites(record, script_id, parsed, match, method, sites, strings):
    evidence = parsed.evidence(method, match.start())
    available = sites.get(script_id)
    if not available:
        if script_id.startswith("conversation."):
            # A conversation is a source for the grant, but its attachment and
            # speaker must be established independently.
            available = [place("conversation", evidence)]
            names = set(re.findall(r'\bsetName\s*\(\s*(?:npc|self)\s*,\s*"([^"]+)"', parsed.text))
            if len(names) == 1:
                available[0]["npc"] = {"id": script_id, **strings.label(next(iter(names)))}
            for candidate in PLANETS:
                if parsed.path.stem.startswith(candidate + "_"):
                    available[0]["planet_hint"] = candidate
                    available[0]["planet_hint_basis"] = "conversation_identifier"
                    break
        else:
            available = [place("script", evidence)]
    for site in available:
        add_unique(record["starts"], {**site, "grant": evidence, "script": script_id})
    return evidence


def scan_scripts(records, pending, strings, scripts, sites, files):
    stats = {"effective_script_files": len(scripts), "source_files_with_literal_calls": 0,
             "unresolved_script_targets": [], "unresolved_script_call_counts": {}}
    unresolved = defaultdict(int)
    missing_targets = set()
    for script_id, path in sorted(scripts.items()):
        if TEST_ID.search(script_id.replace(".", "/")) or script_id.startswith(("content_tools.", "developer.")):
            continue
        text = files.text(path)
        if not CALL.search(text):
            continue
        parsed = Script(path, text)
        found = False
        for method_name, method in parsed.methods.items():
            matches = list(CALL.finditer(method.body))
            if not matches:
                continue
            symbols = {**parsed.constants, **assignments(method.body)}
            payouts = [
                concrete_quest(arguments(method.body, match.end() - 1), "space_quest", "giveReward", symbols)
                for match in matches if match.groups() == ("space_quest", "giveReward")
            ]
            for match in matches:
                module, name = match.groups()
                if module is None and name not in ("questActivateQuest", "requestActivateQuest"):
                    continue
                args = arguments(method.body, match.end() - 1)
                ident = concrete_quest(args, module, name, symbols)
                if ident not in records:
                    if ident is None:
                        unresolved[f"{module + '.' if module else ''}{name}"] += 1
                    elif ident not in missing_targets:
                        missing_targets.add(ident)
                        stats["unresolved_script_targets"].append(
                            {"id": ident, "source": parsed.evidence(method, match.start())})
                    continue
                found = True
                record = records[ident]
                if record["source_status"] == "reference_asset":
                    record["source_status"] = "script_referenced_asset"
                if name == "giveReward" and module == "space_quest":
                    add_space_reward(record, args, symbols, parsed.evidence(method, match.start()),
                                     branch=payouts.count(ident) > 1)
                    continue
                evidence = add_script_sites(record, script_id, parsed, match, method, sites, strings)
                gates = list(conditions(parsed, method_name, match.start()))
                if script_id.startswith("conversation."):
                    gates.extend(dialogue_conditions(parsed, method_name, match.start()))
                for expression, origin in gates:
                    requirement(record, "script_route_guard", origin, expression=expression, grant=evidence)
                    for previous, predicate in required_completion(expression, symbols):
                        if previous != ident:
                            edge(pending, previous, ident, "script_route_requires_completion",
                                 origin, predicate=predicate, grant=evidence)
            if payouts and len(set(payouts)) == 1 and payouts[0] in records:
                add_space_reward_extras(records[payouts[0]], parsed, method, symbols)
        if found:
            stats["source_files_with_literal_calls"] += 1
    stats["unresolved_script_call_counts"] = dict(sorted(unresolved.items()))
    stats["unresolved_script_targets"].sort(key=lambda entry: entry["id"])
    return stats


def add_space_reward(record, args, symbols, evidence, branch=False):
    if len(args) < 4:
        return
    amount = literal(args[3], symbols)
    selection = {"selection": "script_branch"} if branch else {}
    if isinstance(amount, int) and amount > 0:
        reward(record, "credits", "script_turn_in", evidence, amount=amount, destination="bank", **selection)
    elif not isinstance(amount, int):
        reward(record, "credits", "script_turn_in", evidence, mode="script_expression", expression=args[3])
    if len(args) > 4 and args[4] != "null":
        item = literal(args[4], symbols)
        if isinstance(item, str) and item:
            reward(record, "item", "script_turn_in", evidence, item_id=item, count=1,
                   item_type="template", **selection)
        else:
            reward(record, "item", "script_turn_in", evidence, mode="script_expression", expression=args[4])


def add_space_reward_extras(record, parsed, method, symbols):
    pattern = r"\b(factions\.addFactionStanding|badge\.grantBadge|skill\.noisyGrantSkill)\s*\("
    for match in re.finditer(pattern, method.body):
        args = arguments(method.body, match.end() - 1)
        if len(args) < 2:
            continue
        evidence = parsed.evidence(method, match.start())
        key = literal(args[1], symbols)
        if match[1] == "factions.addFactionStanding" and len(args) == 3:
            known = re.fullmatch(r"factions\.FACTION_(\w+)", args[1])
            faction = key or (known[1].title() if known else args[1])
            amount = literal(args[2], symbols)
            if isinstance(amount, (int, float)) and amount != 0:
                if isinstance(amount, float) and amount.is_integer():
                    amount = int(amount)
                reward(record, "faction", "script_turn_in", evidence, faction=faction, amount=amount)
        elif match[1] == "badge.grantBadge":
            reward(record, "badge", "script_turn_in", evidence, item_id=key or args[1])
        elif match[1] == "skill.noisyGrantSkill":
            reward(record, "skill", "script_turn_in", evidence, item_id=key or args[1])
