"""Index SWG sound templates, playable samples, and authored uses."""

from collections import defaultdict
from functools import lru_cache
import json
from pathlib import PurePosixPath
import re
import struct

from catalog_client_assets import resolve_asset_path
from catalog_entities import item_ref
from catalog_models import chunks
from catalog_sources import ROOT, SHARED, Script, arguments, display_id, literal, read_iff_table, script_paths, source, string_ref


AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac"}
SOUND_DIRS = ("sound", "sample", "voice", "music", "player_music")
SOUND_REF = re.compile(r"(?<![\w/])(?:sound|sample|voice|music|player_music)/[a-z0-9_/ .-]+\.(?:snd|wav|mp3|ogg|flac)\b", re.I)
CALL = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
PLAY_CALLS = {"play2dnonloopingsound", "play2dloopingsound", "playsound", "playmusic"}
TEXT_CALL = re.compile(r"\bmessage\s*=\s*(?:new\s+)?string_id\s*\(")
BLOCK = re.compile(r'''"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[{}]''')
POWER_AUDIO = "datatables/space/ship_power_sounds.iff"


def sound_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip().replace("\\", "/").lower()
    if SOUND_REF.fullmatch(value) and not any(part in ("", ".", "..") for part in value.split("/")):
        return value
    return None


def sound_refs(text):
    for match in SOUND_REF.finditer(text.replace("\\", "/")):
        if ident := sound_id(match[0]):
            yield ident


def template_samples(data):
    """Read the ordered sample list in native SD2D or SD3D sound templates."""
    info = next((entry for entry in chunks(memoryview(data)) if entry.form and entry.tag in ("SD2D", "SD3D")), None)
    if info is None:
        raise ValueError("Unrecognized sound template.")
    if info.tag == "SD3D":
        info = next((entry for entry in chunks(info.body) if entry.form and entry.tag == "SD2D"), None)
    version = next((entry for entry in chunks(info.body) if not entry.form and entry.tag in
                    ("0000", "0001", "0002", "0003", "0004")), None) if info else None
    if version is None:
        raise ValueError("Unrecognized sound template version.")
    body = bytes(version.body)
    count, = struct.unpack_from("<I", body)
    if count > len(body) - 4:
        raise ValueError("Invalid sound sample count.")
    cursor = 4
    samples = []
    for _ in range(count):
        stop = body.index(b"\0", cursor)
        if ref := sound_id(body[cursor:stop].decode("ascii")):
            samples.append(ref)
        cursor = stop + 1
    return list(dict.fromkeys(samples))


def played_sounds(parsed, method, symbols):
    for call in CALL.finditer(method.body):
        if call[1].lower() in PLAY_CALLS:
            for arg in arguments(method.body, call.end() - 1):
                if ident := sound_id(literal(arg, symbols)):
                    yield call.start(), ident, parsed.evidence(method, call.start())


def audio_for_lines(body, lines, cues):
    """Match explicit playback calls to the single identified line within each script block."""
    if not lines or not cues:
        return
    events = [(offset, "sound", (audio, proof)) for offset, audio, proof in cues]
    events.extend((offset, "line", line) for offset, line in lines)
    events.extend((token.start(), token[0], None) for token in BLOCK.finditer(body)
                  if token[0] in ("{", "}"))
    scopes = [defaultdict(list)]
    completed = []
    for _, event, value in sorted(events, key=lambda row: row[0]):
        if event == "{":
            scopes.append(defaultdict(list))
        elif event == "}" and len(scopes) > 1:
            completed.append(scopes.pop())
        else:
            scopes[-1][event].append(value)
    for scoped in [*completed, *scopes]:
        matched = set(scoped["line"])
        if len(matched) == 1:
            line = next(iter(matched))
            for ident, proof in scoped["sound"]:
                yield line, ident, proof


def voiced_lines(parsed, symbols, default):
    """Resolve voice cues authored by generated conversation scripts."""
    played = {name: list(played_sounds(parsed, method, symbols)) for name, method in parsed.methods.items()}
    for name, method in parsed.methods.items():
        cues = list(played[name])
        for call in CALL.finditer(method.body):
            if call[1] in played and call[1] != name:
                cues.extend((call.start(), audio, proof) for _, audio, proof in played[call[1]])
        if not cues:
            continue
        lines = []
        for call in TEXT_CALL.finditer(method.body):
            args = arguments(method.body, call.end() - 1)
            if len(args) != 2:
                continue
            table, key = (literal(arg, symbols) for arg in args)
            if table is None and args[0] == "c_stringFile":
                table = default
            if isinstance(table, str) and isinstance(key, str):
                if ref := string_ref(f"@{table}:{key}"):
                    lines.append((call.start(), ref))
        for (table, key), sound, proof in audio_for_lines(method.body, lines, cues):
            yield table, key, sound, proof


def _music_cue(ident):
    return ident.startswith(("music/", "player_music/")) or PurePosixPath(ident).name.startswith(("mus_", "music_"))


def _alternate_conversation_contacts(db, voiced_tables):
    """Find script-only contacts whose named model has one authored conversation variant."""
    contacts = defaultdict(list)
    variants = defaultdict(set)
    for npc, name, category, model, table, script in db.execute("""
        SELECT e.id,e.name,e.category,json_extract(e.data,'$.model_asset'),l.target_id,
               json_extract(l.detail,'$.script')
        FROM entity e JOIN link l ON l.kind=e.kind AND l.id=e.id
        WHERE e.kind='npcs' AND l.role='dialogue' AND l.target_kind='conversations'
    """):
        if not model or not table.startswith("conversation/") or script != table.replace("/", "."):
            continue
        stem = re.sub(r"_?\d+$", "", script)
        if category == "Quest contact":
            named_model = display_id(PurePosixPath(model).stem.removeprefix("shared_"))
            if npc == script and table not in voiced_tables and name.casefold() == named_model.casefold():
                contacts[model, script].append(npc)
        elif stem != script:
            variants[model, stem].add((npc, table))

    matched = defaultdict(list)
    for key, legacy_ids in contacts.items():
        choices = variants.get(key, ())
        if len(choices) == 1:
            npc, table = next(iter(choices))
            if table in voiced_tables:
                matched[npc, table].extend(legacy_ids)
    return matched


class SoundLibrary:
    def __init__(self, clientdata):
        self.root = resolve_asset_path(clientdata)

    def path(self, value):
        if ident := sound_id(value):
            path = (self.root / ident).resolve()
            if path.is_relative_to(self.root) and path.is_file():
                return path
        return None

    def clip(self, value):
        if (suffix := PurePosixPath(value).suffix.lower()) in AUDIO_TYPES and (path := self.path(value)):
            try:
                return AUDIO_TYPES[suffix], path.read_bytes()
            except OSError:
                pass
        return None


class SoundIndex:
    def __init__(self, clientdata, models):
        self.library = SoundLibrary(clientdata)
        self.models = models
        self.records = {}
        self.line_audio = defaultdict(list)

    def ensure(self, ident):
        if ident not in self.records:
            path = self.library.path(ident)
            suffix = PurePosixPath(ident).suffix
            self.records[ident] = {
                "id": ident, "name": display_id(ident), "format": suffix[1:].upper(),
                "available": bool(path), "samples": [],
                "sources": [{"path": "clientdata/" + ident}] if path else [],
            }
        return self.records[ident]

    def inventory(self):
        for directory in SOUND_DIRS:
            for path in (self.library.root / directory).rglob("*"):
                if path.suffix.lower() not in {".snd", *AUDIO_TYPES} or not path.is_file():
                    continue
                if ident := sound_id(path.relative_to(self.library.root).as_posix()):
                    self.ensure(ident)
        for ident in tuple(self.records):
            if not ident.endswith(".snd"):
                continue
            path = self.library.path(ident)
            try:
                samples = template_samples(path.read_bytes()) if path else []
            except (OSError, ValueError, UnicodeError, struct.error):
                self.records[ident]["unreadable"] = True
                continue
            self.records[ident]["samples"] = samples
            for sample in samples:
                self.ensure(sample)

    def clip_ids(self, ident):
        record = self.ensure(ident)
        if not ident.endswith(".snd"):
            return [ident] if record["available"] else []
        return [sample for sample in record["samples"]
                if PurePosixPath(sample).suffix in AUDIO_TYPES and self.ensure(sample)["available"]]

    def use(self, writer, ident, kind, target, proof, description=None):
        evidence = {"source": proof, "reason": description}
        for sound in dict.fromkeys((ident, *self.ensure(ident)["samples"])):
            self.ensure(sound)
            writer.link("sounds", sound, kind, kind, target, evidence)
            writer.link(kind, target, "sounds", "sounds", sound, evidence)

    def dialogue(self, writer, dialogue, comms=()):
        owners = defaultdict(list)
        for kind, ident, table in writer.connection.execute(
            """SELECT kind,id,target_id FROM link WHERE role='dialogue' AND target_kind='conversations'
               AND json_type(detail,'$.speech_keys') IS NULL"""
        ):
            owners[table].append((kind, ident))
        voiced_tables = {table for (table, _), cues in dialogue.audio.items()
                         if any(not _music_cue(ident) for ident, _ in cues)}
        alternatives = _alternate_conversation_contacts(writer.connection, voiced_tables)
        recordings = defaultdict(dict)

        def cue(table, key, ident, proof, npc=None):
            clip = next(iter(self.clip_ids(ident)), None)
            entry = {"id": ident, "clip": clip, "source": proof}
            if npc:
                entry["npc"] = npc
            self.line_audio[table, key].append(entry)
            code = f"@{table}:{key}"
            text = dialogue.strings.table(table)[0].get(key)
            line = {"table": table, "code": code, "text": text, "source": proof}
            if _music_cue(ident):
                entry["music"] = line["music"] = True
            clips = dict.fromkeys((ident, *self.ensure(ident)["samples"]))
            for sound in clips:
                recordings["conversations", table, sound][code] = line
            return line, clips

        for (table, key), cues in dialogue.audio.items():
            for ident, proof in cues:
                line, clips = cue(table, key, ident, proof)
                for kind, target in owners[table]:
                    for sound in clips:
                        recordings[kind, target, sound][line["code"]] = line
                        if kind == "npcs" and not line.get("music"):
                            for contact in alternatives.get((target, table), ()):
                                recordings[kind, contact, sound].setdefault(line["code"], {**line, "recorded_npc": target})
        for npc, ref, ident, proof in comms:
            if ref:
                line, clips = cue(*ref, ident, proof, npc)
                for sound in clips:
                    recordings["npcs", npc, sound][line["code"]] = {**line, "npc": npc}
        for (kind, target, sound), lines in recordings.items():
            linked_lines = [lines[code] for code in sorted(lines)]
            evidence = {"reason": "Recorded dialogue", "source": linked_lines[0]["source"],
                        "dialogue_lines": linked_lines}
            if all(line.get("music") for line in linked_lines):
                evidence["reason"] = "Music cue"
            elif all(line.get("recorded_npc") for line in linked_lines):
                evidence["reason"] = "Recorded in alternate conversation"
            writer.link("sounds", sound, kind, kind, target, evidence)
            writer.link(kind, target, "sounds", "sounds", sound, evidence)
        for npc, ref, ident, proof in comms:
            if ref is None:
                self.use(writer, ident, "npcs", npc, proof, "Comm audio")

    def authored(self, writer, files):
        bindings = defaultdict(list)
        scripts_by_entity = defaultdict(list)
        for kind, ident, raw in writer.connection.execute(
            "SELECT kind,id,data FROM entity WHERE kind IN ('quests','npcs','items','ships','locations')"
        ):
            data = json.loads(raw)
            for proof in data.get("sources", []):
                path = proof.get("path", "") if isinstance(proof, dict) else ""
                if not path.startswith("dsrc/"):
                    continue
                suffix = PurePosixPath(path).suffix.lower()
                if suffix in (".script", ".scriptlib", ".java", ".tab", ".tpf"):
                    bindings[path].append((kind, ident, proof.get("line") if suffix == ".tab" else None))
            scripts = data.get("scripts") or ()
            if isinstance(scripts, str):
                scripts = (scripts,)
            for script in scripts:
                if isinstance(script, str):
                    scripts_by_entity[script].append((kind, ident, None))
            template = data.get("client_template") or data.get("model_asset") or data.get("template")
            if isinstance(template, str) and template.startswith("object/"):
                description = "Character model sound" if kind == "npcs" else "Client object sound"
                for sound, proof in self.template_sounds(template):
                    self.use(writer, sound, kind, ident, proof, description)

        for script, path in script_paths(files).items():
            owners = scripts_by_entity.pop(script, ())
            bindings[source(path)["path"]].extend(owners)
        for raw_path, owners in bindings.items():
            path = ROOT / raw_path
            if not files.exists(path):
                continue
            text = files.text(path)
            if not text or not SOUND_REF.search(text.replace("\\", "/")):
                continue
            if path.suffix in (".java", ".script", ".scriptlib"):
                text = Script(path, text).text
            for line, text_row in enumerate(text.splitlines(), 1):
                for sound in dict.fromkeys(sound_refs(text_row)):
                    record = self.ensure(sound)
                    proof = source(path, line)
                    if not owners and len(record["sources"]) < 24:
                        record["sources"].append(proof)
                    for kind, ident, row in owners:
                        if row is None or row == line:
                            self.use(writer, sound, kind, ident, proof, "Authored sound reference")

    @lru_cache(maxsize=8192)
    def template_sounds(self, template):
        sources = {}
        try:
            selected, fields = self.models._object_appearance(template)
            cdf = fields.get("clientDataFile")
            if cdf and cdf.startswith("clientdata/"):
                data = self.models._blob(cdf, "clientdata/")
                for sound in sound_refs(data.decode("ascii", errors="replace")):
                    sources.setdefault(sound, {"path": "clientdata/" + cdf})
        except (OSError, ValueError, IndexError, struct.error):
            pass
        return tuple(sources.items())


    def engines(self, writer, files):
        components = {}
        table = SHARED / "space/ship_components.tab"
        for row in files.tab(table):
            if target := item_ref(row.get("object_template")):
                components[row.get("name")] = target
        ships = {ident for ident, in writer.connection.execute("SELECT id FROM entity WHERE kind='ships'")}
        for ship in sorted(ships):
            filename = f"datatables/space/ship_chassis_{ship}.iff"
            try:
                _, _, rows = read_iff_table(self.library.root / filename)
            except (OSError, ValueError, KeyError, AssertionError, struct.error):
                rows = [row.cells for row in files.tab(SHARED / f"space/ship_chassis_{ship}.tab")]
                if rows:
                    filename = (SHARED / f"space/ship_chassis_{ship}.tab").as_posix()
            else:
                filename = "clientdata/" + filename
            for index, row in enumerate(rows, 1):
                engine = components.get(row.get("component", ""))
                for token in str(row.get("engine", "")).split(","):
                    attachment, separator, _ = token.strip().partition(":")
                    if not separator or not re.fullmatch(r"[\w-]+", attachment):
                        continue
                    template = f"object/tangible/ship/attachment/engine/shared_{attachment}.iff"
                    for sound, cdf in self.template_sounds(template):
                        proof = {"path": filename, "row": index, "attachment": template, "client_data": cdf["path"]}
                        self.use(writer, sound, "ships", ship, proof, "Engine attachment")
                        if engine:
                            self.use(writer, sound, "items", engine, proof, f"Engine attachment on {display_id(ship)}")

        try:
            _, _, rows = read_iff_table(self.library.root / POWER_AUDIO)
        except (OSError, ValueError, KeyError, AssertionError, struct.error):
            rows = []
        for index, row in enumerate(rows, 1):
            ship = row.get("type")
            for column, label in (("engine_disabled", "Engine power down"), ("engine_enabled", "Engine power up")):
                if ship in ships and (ident := sound_id(row.get(column))):
                    self.use(writer, ident, "ships", ship, {"path": "clientdata/" + POWER_AUDIO, "row": index}, label)

    def publish(self, writer, report_issue=None):
        for ident, record in sorted(self.records.items()):
            if report_issue:
                if not record["available"]:
                    report_issue("Sounds", f"Missing referenced client sound {ident}.")
                elif record.get("unreadable"):
                    report_issue("Sounds", f"Could not read the sample list in {ident}.")
            for sample in record["samples"]:
                proof = {"source": {"path": "clientdata/" + ident}}
                writer.link("sounds", ident, "samples", "sounds", sample, proof)
                writer.link("sounds", sample, "templates", "sounds", ident, proof)
            record["playable"] = self.clip_ids(ident)
            category = "Sound template" if ident.endswith(".snd") else "Audio sample"
            writer.entity("sounds", ident, record["name"], record, category=category,
                          status="available" if record["available"] else "referenced",
                          card={"format": record["format"], "playable": len(record["playable"])})
