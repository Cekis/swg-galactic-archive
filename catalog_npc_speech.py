"""Resolve the dialogue spoken by explicitly identified NPCs in authored scripts and sequences."""

from collections import defaultdict
from pathlib import PurePosixPath
import re

from archive_quests.manifest_scripts import assignments, balanced_end
from catalog_sources import SERVER, Script, arguments, clean_table_name, literal, script_paths, source, string_ref
from catalog_sounds import audio_for_lines, played_sounds, sound_id


COMMS = re.compile(r"\b(?:commTutorialPlayer|commPlayers|sendDelayed3poPopup|attachScript|registerSequenceObject)\s*\(")
CALL = re.compile(r"(?<![\w.])(?:(\w+)\s*\.\s*)?(\w+)\s*\(")
CREATION = re.compile(
    r"\b(\w+)\s*=\s*((?:create\s*\.\s*(?:object|objectWithYaw|createNpc|staticObject)"
    r"|createObject|createObjectAt|createObjectInCell)\s*\()"
)
RECEIVER = re.compile(r"\b(\w+)\s*=\s*(?:(?:sequencer|utils)\s*\.\s*)?(getSequenceObject|getObjIdScriptVar)\s*\(")
TEXT_VALUE = re.compile(r"\b(\w+)\s*=\s*((?:new\s+)?string_id|prose\s*\.\s*getPackage)\s*\(")
TEXT_CALL = re.compile(r"(?:new\s+)?string_id\s*\(")
PROSE_CALL = re.compile(r"prose\s*\.\s*getPackage\s*\(")
STRING_CPP = re.compile(r"\b(\w+)\s*=\s*getStringObjVar\s*\(")
SPAWN_FIELDS = re.compile(r'"(spawns|quest_script)"\s*=\s*"([^"]+)"')
SEQUENCE_TABLES = SERVER / "sequencer/event_sequence"
STATIC_NPC = SERVER.parent / "object/tangible/spawning/static_npc"
COMM_BEEPS = {"sound/sys_comm_generic.snd", "sound/sys_comm_other.snd"}


def _ref(table, key):
    return string_ref(f"@{table}:{key}") if isinstance(table, str) and isinstance(key, str) else None


def _one(values):
    return next(iter(values)) if len(values) == 1 else None


def _owned_npcs(db):
    ids = set()
    templates = defaultdict(set)
    for ident, model in db.execute("""
        SELECT id,json_extract(data,'$.model_asset') FROM entity
        WHERE kind='npcs' AND category<>'Quest contact'
    """):
        ids.add(ident.casefold())
        if model:
            templates[model.casefold()].add(ident)

    def resolve(value):
        if not isinstance(value, str):
            return None
        value = value.casefold()
        if value.startswith("object/mobile/") and value.endswith(".iff"):
            name = PurePosixPath(value).stem.removeprefix("shared_")
            return name if name in ids else _one(templates[value])
        return value if value in ids else None

    return resolve


def _text_ref(expression, values, refs, sequence_keys, depth=0):
    if depth >= 4:
        return None
    if expression in refs:
        return _one(refs[expression])
    if text := literal(expression, values):
        if isinstance(text, str):
            return string_ref(text)
        return None
    if match := TEXT_CALL.match(expression):
        args = arguments(expression, match.end() - 1)
        if len(args) == 2:
            key = literal(args[1], values)
            table = literal(args[0], values)
            if args[0] in sequence_keys and isinstance(key, str):
                table = _one(sequence_keys[args[0]].get(key, ()))
            return _ref(table, key)
    if match := PROSE_CALL.match(expression):
        args = arguments(expression, match.end() - 1)
        if args:
            return _text_ref(args[0], values, refs, sequence_keys, depth + 1)
    return None


def _bound_text(body, values, inherited, sequence_index):
    sequence_keys = {}
    for match in STRING_CPP.finditer(body):
        args = arguments(body, match.end() - 1)
        if len(args) == 2 and literal(args[1], values) == "strSequenceTable":
            sequence_keys[match[1]] = sequence_index
    refs = defaultdict(set, {key: set(entries) for key, entries in inherited.items()})
    for match in TEXT_VALUE.finditer(body):
        ref = _text_ref(body[match.start(2):], values, refs, sequence_keys)
        if ref:
            refs[match[1]].add(ref)
    return refs, sequence_keys


def _created(body, values, resolve):
    actors = defaultdict(set)
    for match in CREATION.finditer(body):
        args = arguments(body, match.end() - 1)
        if args and (npc := resolve(literal(args[0], values))):
            actors[match[1]].add(npc)
    return actors


def _calls(body):
    for call in CALL.finditer(body):
        if call[2] in ("registerSequenceObject", "setScriptVar", "setObjIdScriptVar", "attachScript",
                       "commTutorialPlayer", "commPlayers", "sendDelayed3poPopup", "chat"):
            yield call, arguments(body, call.end() - 1)


def _recording(sound):
    ident = sound_id(sound)
    return ident if ident not in COMM_BEEPS else None


def _registered_actors(parsed, created):
    roles, variables = defaultdict(set), defaultdict(set)
    for name, method in parsed.methods.items():
        values = {**parsed.constants, **assignments(method.body)}
        for call, args in _calls(method.body):
            if call[2] == "registerSequenceObject" and len(args) == 2 and isinstance(key := literal(args[1], values), str):
                roles[key].update(created[name][args[0]])
            if call[2] in ("setScriptVar", "setObjIdScriptVar") and len(args) == 3 and args[0] == "self":
                key = literal(args[1], values)
                if isinstance(key, str):
                    variables[key].update(created[name][args[2]])
    return roles, variables


def _script_audio(parsed, resolve, sequence_index, owner=None):
    fields = parsed.text[:min((method.offset for method in parsed.methods.values()), default=len(parsed.text))]
    external, _ = _bound_text(fields, parsed.constants, {}, sequence_index)
    created = {name: _created(method.body, {**parsed.constants, **assignments(method.body)}, resolve)
               for name, method in parsed.methods.items()}
    roles, variables = _registered_actors(parsed, created)
    for name, method in parsed.methods.items():
        values = {**parsed.constants, **assignments(method.body)}
        refs, sequence_keys = _bound_text(method.body, values, external, sequence_index)
        actors = created[name]
        spoken = []
        if owner:
            actors["self"].add(owner)
        for match in RECEIVER.finditer(method.body):
            args = arguments(method.body, match.end() - 1)
            if args and isinstance(key := literal(args[-1], values), str):
                choices = roles if match[2] == "getSequenceObject" else variables
                actors[match[1]].update(choices.get(key, ()))
        for call, args in _calls(method.body):
            function = call[2]
            proof = parsed.evidence(method, call.start())
            if function == "attachScript" and len(args) == 2:
                npc = _one(actors[args[0]])
                script = literal(args[1], values)
                if npc and isinstance(script, str) and script.startswith("conversation."):
                    yield "attachment", (npc, script, proof)
            elif function in ("commTutorialPlayer", "commPlayers") and len(args) == 6:
                transcript, audio, appearance = (3, 4, 5) if function == "commTutorialPlayer" else (5, 2, 1)
                npc = resolve(literal(args[appearance], values)) or _one(actors[args[0]])
                ref = _text_ref(args[transcript], values, refs, sequence_keys)
                if npc:
                    yield "speech", (npc, ref, _recording(literal(args[audio], values)), proof)
            elif function == "sendDelayed3poPopup" and len(args) == 7:
                npc = resolve("c_3po")
                ref = _ref(literal(args[4], values), literal(args[5], values))
                if npc:
                    yield "speech", (npc, ref, _recording(literal(args[3], values)), proof)
            elif function == "chat" and call[1] == "chat" and args:
                npc = _one(actors[args[0]])
                ref = _one({ref for arg in args[1:] if (ref := _text_ref(arg, values, refs, sequence_keys))})
                if npc and ref:
                    spoken.append((call.start(), (npc, ref)))
                    yield "speech", (npc, ref, None, proof)
        if spoken and parsed.path.parent.name != "conversation":
            cues = list(played_sounds(parsed, method, values))
            for (npc, ref), sound, proof in audio_for_lines(method.body, spoken, cues):
                if sound := _recording(sound):
                    yield "speech", (npc, ref, sound, proof)
    for role, actors in roles.items():
        for actor in actors:
            yield "role", (role, actor)


def _sequencer_actors(parsed, resolve):
    """Read named appearance overrides from the sequencer's action branches."""
    action = re.compile(r'\bstrAction\s*\.\s*equals\s*\(\s*"(\w+)"\s*\)\s*\)\s*\{')
    dispatcher = re.compile(r"\bswitch\s*\(\s*strAction(?:\s*\.\s*toLowerCase\s*\(\s*\))?\s*\)\s*\{")
    cases = re.compile(r'\bcase\s+"(\w+)"\s*:\s*(.*?)(?=\b(?:case\s+"|default\s*:)|\Z)', re.S)
    branches = []
    for branch in action.finditer(parsed.text):
        start = branch.end() - 1
        branches.append((branch[1], parsed.text[start + 1:balanced_end(parsed.text, start)]))
    for branch in dispatcher.finditer(parsed.text):
        start = branch.end() - 1
        branches.extend(cases.findall(parsed.text[start + 1:balanced_end(parsed.text, start)]))
    result = {}
    for name, body in branches:
        npcs = {npc for call, args in _calls(body) if call[2] == "commTutorialPlayer" and len(args) == 6
                and (npc := resolve(literal(args[5], parsed.constants)))}
        if npc := _one(npcs):
            result[name.casefold()] = npc
    return result


def index_npc_speech(writer, files, dialogue):
    resolve = _owned_npcs(writer.connection)
    paths = script_paths(files)
    by_script = defaultdict(set)
    for ident, script in writer.connection.execute("""
        SELECT e.id,s.value FROM entity e,json_each(e.data,'$.scripts') s
        WHERE e.kind='npcs' AND e.category<>'Quest contact' AND s.type='text'
    """):
        by_script[script].add(ident)
    sequence_index = defaultdict(set)
    rows = []
    for path in files.paths(SEQUENCE_TABLES, "*.tab"):
        table = clean_table_name(path.stem)
        if not table:
            continue
        strings = dialogue.strings.table(table)[0]
        for key in strings:
            sequence_index[key].add(table)
        rows.extend((table, row) for row in files.tab(path))

    speech_links = defaultdict(dict)
    audio_cues = {}
    roles = defaultdict(set)
    fixed_roles = {}

    def attach(npc, script, proof):
        for table in sorted(dialogue.script_tables.get(script, ())):
            evidence = {"script": script, "reason": "Authored NPC script attachment", "source": proof}
            writer.link("npcs", npc, "dialogue", "conversations", table, evidence)
            writer.link("conversations", table, "npcs", "npcs", npc, evidence)

    def add(npc, ref, audio, proof):
        if ref:
            table, key = ref
            speech_links[npc, table].setdefault(key, proof)
            usages = dialogue.init_table(table)[key]
            if not any(use.get("source") == proof for use in usages):
                usages.append({"source": proof, "speaker": "npc", "source_role": "scripted speech"})
        if audio:
            audio_cues.setdefault((npc, ref, audio), proof)

    for ident, path in sorted(paths.items()):
        if any(part in ("test", "working", "beta") for part in path.parts):
            continue
        raw = files.text(path)
        owner = _one(by_script[ident]) if ident in by_script else None
        if not (COMMS.search(raw) or "chat.chat" in raw):
            continue
        parsed = Script(path, raw)
        if ident == "content_tools.sequencer_master_object":
            fixed_roles = _sequencer_actors(parsed, resolve)
        for kind, evidence in _script_audio(parsed, resolve, sequence_index, owner):
            if kind == "attachment":
                attach(*evidence)
            elif kind == "speech":
                add(*evidence)
            else:
                role, npc = evidence
                roles[role].add(npc)

    for path in files.paths(STATIC_NPC, "*.tpf", recursive=True):
        raw = files.text(path)
        fields = {match[1]: match for match in SPAWN_FIELDS.finditer(raw)}
        if "spawns" not in fields or "quest_script" not in fields:
            continue
        match = fields["quest_script"]
        if (npc := resolve(fields["spawns"][2])) and match[2].startswith("conversation."):
            attach(npc, match[2], source(path, raw.count("\n", 0, match.start()) + 1))

    for table, row in rows:
        action = row.get("strAction").casefold()
        if action not in fixed_roles and action not in ("saysound", "say", "spacechat"):
            continue
        npc = fixed_roles.get(action) or _one(roles[row.get("strActor")])
        key = row.get("strData1")
        if npc and (ref := _ref(table, key)):
            audio = _recording(row.get("strData2")) if action != "say" else None
            add(npc, ref, audio, row.source)
            if action == "wookieecomm" and key + "_w" in dialogue.strings.table(table)[0]:
                add(npc, _ref(table, key + "_w"), audio, row.source)

    for (npc, table), keys in speech_links.items():
        detail = {"reason": "NPC speech and comms", "speech_keys": sorted(keys),
                  "source": next(iter(keys.values()))}
        writer.link("npcs", npc, "dialogue", "conversations", table, detail)
        writer.link("conversations", table, "npcs", "npcs", npc, detail)

    return [(npc, ref, sound, proof) for (npc, ref, sound), proof in audio_cues.items()]
