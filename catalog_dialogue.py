"""Index conversation references with their exact client strings and source captions."""

from collections import defaultdict
from pathlib import PurePosixPath
import re

from catalog_sounds import PLAY_CALLS, voiced_lines
from catalog_sources import (
    SCRIPTS, Script, arguments, clean_table_name, display_id, literal, script_paths, source, string_ref,
)


STRING_CALL = re.compile(r"\b(?:new\s+)?string_id\s*\(")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
RESPONSE = re.compile(r'\b(?:response|responseId|responseKey)\s*(?:==\s*"([^"]+)"|\.equals\s*\(\s*"([^"]+)")')
SPACE_CALL = re.compile(r"\bspace_quest\s*\.\s*\w+\s*\(")
CAPTION = re.compile(r"^[ \t]*//[ \t-]*(NPC|PLAYER):[ \t]*(.*)$", re.IGNORECASE)
SIMPLE_CONST = re.compile(r'\b(?:String|string)\s+(\w+)\s*=\s*("(?:[^"\\]|\\.)*")\s*;')
TOPICS = ("conversation/", "static_npc/", "epic_quest/", "theme_park", "npc_mission/",
          "random_quest/", "newbie_tutorial/", "newbie_handoff/")


def normalize_line(value):
    return " ".join(value.split())


def stable_ref(table, key):
    return f"@{table}:{key}"


def caption_at(lines, row):
    for idx in range(row - 2, max(-1, row - 6), -1):
        line = lines[idx]
        caption = CAPTION.match(line)
        if caption:
            return caption[1].lower(), caption[2].strip()
        if line.strip() and not line.lstrip().startswith("//"):
            break
    return None, None


class DialogueIndex:
    def __init__(self, strings, quest_ids):
        self.strings = strings
        self.quest_ids = set(quest_ids)
        self.quest_aliases = defaultdict(set)
        for ident in quest_ids:
            if ident.startswith(("quest/", "legacy/journal/")):
                self.quest_aliases[ident.split("/")[-1]].add(ident)
            self.quest_aliases[ident].add(ident)
        self.tables = {name: defaultdict(list) for name in strings.conversation_names()}
        self.dynamic = defaultdict(list)
        self.audio = defaultdict(list)
        self.quest_tables = defaultdict(dict)
        self.script_tables = defaultdict(set)

    def init_table(self, table):
        return self.tables.setdefault(table, defaultdict(list))

    def link(self, quest_id, table, reason, evidence):
        if table not in self.quest_tables[quest_id]:
            self.quest_tables[quest_id][table] = {"reason": reason, "source": evidence}

    def direct(self, ident, value, evidence, role="system"):
        ref = string_ref(value)
        if not ref:
            return
        table, key = ref
        self.init_table(table)[key].append({"source": evidence, "speaker": role, "source_role": "table"})
        self.link(ident, table, "quest text reference", evidence)

    def scan(self, files):
        paths = script_paths(files)
        for script_id, path in sorted(paths.items()):
            raw = files.text(path)
            conversation_file = script_id.startswith("conversation.")
            if not conversation_file and not any(prefix in raw for prefix in TOPICS):
                continue
            matched_tables = self.scan_script(script_id, path, raw, "effective", conversation_file)
            if matched_tables and path.suffix == ".java":
                for suffix in (".script", ".scriptlib"):
                    companion = path.with_suffix(suffix)
                    if files.exists(companion):
                        self.scan_script(script_id, companion, files.text(companion), "companion", conversation_file)
        self.strings.prefetch(self.tables)

    def scan_script(self, script_id, path, raw, source_role, conversation_file):
        parsed = Script(path, raw)
        lines = raw.splitlines()
        symbols = {**parsed.constants}
        declared = defaultdict(set)
        for name, value in SIMPLE_CONST.findall(parsed.text):
            declared[name].add(value)
        symbols.update({name: next(iter(options)) for name, options in declared.items() if len(options) == 1})
        default = literal("c_stringFile", symbols)
        if not isinstance(default, str) and conversation_file:
            default = "conversation/" + path.stem
        default = clean_table_name(default) if isinstance(default, str) else None
        if any(play in raw.lower() for play in PLAY_CALLS):
            for table, key, sound, proof in voiced_lines(parsed, symbols, default):
                cues = self.audio[table, key]
                if not any(sound == existing for existing, _ in cues):
                    cues.append((sound, proof))
                self.init_table(table)
        seen = set()
        for match in STRING_CALL.finditer(parsed.text):
            args = arguments(parsed.text, match.end() - 1)
            if len(args) != 2:
                continue
            table = literal(args[0], symbols)
            table = clean_table_name(table) if isinstance(table, str) else None
            if table is None and args[0] == "c_stringFile":
                table = default
            if not table or not (table.startswith(TOPICS) or table == default):
                continue
            row = parsed.line(match.start())
            self.init_table(table)
            seen.add(table)
            evidence = source(path, row)
            speaker, caption = caption_at(lines, row)
            if speaker is None:
                prefix = lines[row - 1][:match.start() - (parsed.line_ends[row - 2] + 1 if row > 1 else 0)]
                if "response" in prefix.lower():
                    speaker = "player"
                elif "message" in prefix.lower() and conversation_file:
                    speaker = "npc"
            key = literal(args[1], symbols)
            if isinstance(key, str) and key:
                self.tables[table][key.lower()].append({
                    "source": evidence, "source_role": source_role, "speaker": speaker, "caption": caption,
                })
            else:
                self.dynamic[table].append({"expression": args[1], "source": evidence,
                                            "speaker": speaker, "caption": caption, "source_role": source_role})
        if default and conversation_file:
            for match in RESPONSE.finditer(parsed.text):
                row = parsed.line(match.start())
                speaker, caption = caption_at(lines, row)
                key = (match[1] or match[2]).lower()
                self.init_table(default)[key].append({"source": source(path, row), "source_role": source_role,
                                                      "speaker": speaker or "player", "caption": caption})
                seen.add(default)
        for packed in QUOTED.finditer(parsed.text):
            value = packed[1].replace("\\\\", "/")
            ref = string_ref(value)
            if ref and ref[0].startswith(TOPICS):
                table, key = ref
                row = parsed.line(packed.start())
                speaker, caption = caption_at(lines, row)
                self.init_table(table)[key].append({"source": source(path, row), "source_role": source_role,
                                                   "speaker": speaker, "caption": caption})
                seen.add(table)
        if not seen:
            return seen
        self.script_tables[script_id].update(seen)
        if source_role == "effective":
            self.link_quest_references(parsed, path, symbols, seen)
        return seen

    def link_quest_references(self, parsed, path, symbols, tables):
        refs = {}
        for match in QUOTED.finditer(parsed.text):
            value = match[1].replace("\\\\", "/")
            for quest_id in self.quest_aliases.get(value, ()):
                refs.setdefault(quest_id, source(path, parsed.line(match.start())))
        for match in SPACE_CALL.finditer(parsed.text):
            args = arguments(parsed.text, match.end() - 1)
            if len(args) >= 3:
                kind, name = (literal(arg, symbols) for arg in args[1:3])
                ident = f"spacequest/{kind}/{name}"
                if ident in self.quest_ids:
                    refs.setdefault(ident, source(path, parsed.line(match.start())))
        for ident, evidence in sorted(refs.items()):
            for table in sorted(tables):
                self.link(ident, table, "quest referenced in conversation code", evidence)

    def legacy_table(self, ident, reference, evidence):
        if table := clean_table_name(reference):
            self.init_table(table)
            self.link(ident, table, "conversation table named by quest", evidence)

    def conversation(self, table):
        asset_rows, asset = self.strings.table(table)
        by_key = self.tables[table]
        lines = []
        for key in dict.fromkeys([*asset_rows, *by_key]):
            text = asset_rows.get(key)
            uses = by_key.get(key, [])
            compared = []
            for use in uses:
                caption = use.get("caption")
                if caption is not None and text is not None:
                    aligned = normalize_line(caption) == normalize_line(text)
                    compared.append(aligned)
                    use["alignment"] = "aligned" if aligned else "different"
            if text is None:
                status = "missing"
            elif compared:
                status = "aligned" if all(compared) else "different"
            else:
                status = "uncompared" if uses else "asset_only"
            speakers = sorted({use["speaker"] for use in uses if use.get("speaker")})
            lines.append({"code": stable_ref(table, key), "key": key, "text": text, "status": status,
                          "speakers": speakers, "usages": uses})
        return {"id": table, "name": display_id(PurePosixPath(table).name), "asset": asset,
                "lines": lines, "dynamic": self.dynamic[table],
                "counts": {status: sum(line["status"] == status for line in lines)
                           for status in ("aligned", "different", "missing", "uncompared", "asset_only")}}
