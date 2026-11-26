"""Read a pinned game source revision and the supplied Star Wars Galaxies client assets."""

from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path, PurePosixPath
import re
import struct

from archive_config import PROJECT
from archive_quests.manifest_scripts import Script, arguments, literal, script_paths
from archive_quests.manifest_sources import Files, PLANETS, ROOT, SERVER, SHARED, SCRIPTS, read_stf, source
from catalog_iff import read_iff_table


GAME_MODEL_PATHS = ("sku.0/sys.shared/compiled/game", "sku.0/sys.client/built/game")


SERVER_OBJECTS = SERVER.parent / "object"
SHARED_OBJECTS = SHARED.parent / "object"
ACCEPTED_TEXT = {".tab", ".java", ".script", ".scriptlib", ".tpf"}
RESOURCE_TABLES = {SHARED / "resource/resource_tree.txt", SERVER / "resource/resource_distribution.txt"}
STRING_TABLE = re.compile(r"[a-zA-Z0-9_./-]+")
STRING_ID = re.compile(r"@?([a-zA-Z0-9_./\\-]+):([a-zA-Z0-9_./-]+)")


class GameFiles(Files):
    def __init__(self, repository):
        super().__init__({}, repository.revision)
        roots = (SERVER, SHARED, SCRIPTS, SERVER_OBJECTS, SHARED_OBJECTS)
        resource_paths = {source(path)["path"] for path in RESOURCE_TABLES}
        prefix = repository.subdirectory + "/" if repository.subdirectory else ""
        selected = repository.tree(*(path.relative_to("dsrc").as_posix() for path in roots))
        logical_entries = (("dsrc/" + name.removeprefix(prefix), oid) for name, oid in selected)
        readable_entries = ((name, oid) for name, oid in logical_entries
                            if name in resource_paths or PurePosixPath(name).suffix.lower() in ACCEPTED_TEXT)
        self.blobs.update(repository.blobs(readable_entries))
        if not self.blobs:
            raise ValueError("dsrc has no tracked sku.0 server/shared datatables, scripts or object templates at the selected revision.")


def clean_table_name(value):
    table = value.strip().removesuffix(".stf").replace("\\", "/").lower()
    parts = PurePosixPath(table).parts
    if not STRING_TABLE.fullmatch(table) or not parts or parts[0] == "/" or ".." in parts:
        return None
    return table


def string_ref(value):
    match = STRING_ID.fullmatch(value.strip())
    if match and (table := clean_table_name(match[1])):
        return table, match[2].lower()
    return None


class ClientStrings:
    """Resolve stable string references without changing either asset checkout."""

    def __init__(self, clientdata, client_tools):
        candidates = [
            ("client-tools", client_tools, name) for name in (
                "string/en", "data/string/en", "data/sku.0/sys.shared/built/game/string/en",
                "data/sku.0/sys.client/built/game/string/en",
            )
        ]
        candidates.append(("clientdata", clientdata, "string/en"))
        self.roots = [(label, base, base / name) for label, base, name in candidates if (base / name).is_dir()]
        self.cache = {}
        self.errors = {}

    def _read(self, table):
        relative = PurePosixPath(table + ".stf")
        problem = None
        for label, base, root in self.roots:
            path = root / relative
            try:
                return read_stf(path), f"{label}/{path.relative_to(base).as_posix()}", problem
            except FileNotFoundError:
                continue
            except (ValueError, OSError, struct.error) as error:
                problem = f"{type(error).__name__}: {path.name}"
        return {}, None, problem

    def prefetch(self, names):
        needed = sorted({name for value in names if (name := clean_table_name(value))}
                        .difference(self.cache))
        with ThreadPoolExecutor(max_workers=12) as pool:
            for name, result in zip(needed, pool.map(self._read, needed)):
                self._store(name, result)

    def _store(self, table, result):
        lines, asset, problem = result
        self.cache[table] = (lines, asset)
        if problem:
            self.errors[table] = problem

    def table(self, name):
        table = clean_table_name(name)
        if not table:
            return {}, None
        if table not in self.cache:
            self._store(table, self._read(table))
        return self.cache[table]

    def lookup(self, value):
        ref = string_ref(value)
        if not ref:
            return {"text": value or None}
        table, key = ref
        lines, asset = self.table(table)
        return {"code": f"@{table}:{key}", "table": table, "key": key,
                "text": lines.get(key), "asset": asset}

    def name(self, table, key):
        return self.table(table)[0].get(key.lower())

    def prefetch_labels(self, labels):
        self.prefetch(ref[0] for label in labels if label.startswith("@") and (ref := string_ref(label)))

    def label(self, value):
        if not value.startswith("@"):
            return {"text": value or None}
        resolved = self.lookup(value)
        result = {"key": value, "text": resolved.get("text")}
        if resolved.get("asset"):
            result["asset"] = resolved["asset"]
        return result

    def npc(self, ident):
        for table in ("mob/creature_names", "npc_name"):
            if ident and (label := self.label(f"@{table}:{ident}"))["text"]:
                return {"id": ident, **label}
        return {"id": ident, "text": None}

    def conversation_names(self):
        names = set()
        for _, _, root in self.roots:
            for directory in root.iterdir():
                if not directory.is_dir() or (directory.name not in (
                    "conversation", "static_npc", "random_quest", "epic_quest", "npc_mission",
                    "newbie_tutorial", "newbie_handoff",
                ) and not directory.name.startswith("theme_park")):
                    continue
                for path in directory.rglob("*.stf"):
                    names.add(path.relative_to(root).with_suffix("").as_posix().lower())
        return names


def display_id(ident):
    value = PurePosixPath(ident).stem.replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in value.split()) or ident


def number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            converted = float(value)
            return converted if math.isfinite(converted) else None
        except (TypeError, ValueError):
            return None


def distinct(rows):
    seen = set()
    for row in rows:
        if row not in seen:
            yield row
            seen.add(row)
