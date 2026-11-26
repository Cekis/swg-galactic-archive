"""Read a pinned game source revision and the supplied Star Wars Galaxies client assets."""

from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import struct

PROJECT = Path(__file__).resolve().parent


def game_root():
    configured = os.environ.get("SWG_GAME_ROOT")
    candidates = (Path(configured).expanduser(),) if configured else (
        PROJECT / "game", PROJECT.parent / "whitengold-ai" / "whitengold", PROJECT.parent,
    )
    for candidate in candidates:
        if (candidate / "dsrc").is_dir() and (candidate / "devtools/quests/manifest_sources.py").is_file():
            return candidate.resolve()
    raise RuntimeError("Set SWG_GAME_ROOT to the SWG checkout containing dsrc and the quest manifest tools.")


def asset_root(environment, directory):
    if configured := os.environ.get(environment):
        return Path(configured).expanduser().resolve()
    if ROOT == PROJECT / "game":
        return PROJECT / directory
    return next((parent / directory for parent in (PROJECT, PROJECT.parent, *ROOT.parents)
                 if (parent / directory).is_dir()), PROJECT.parent / directory)


ROOT = game_root()
DEFAULT_CLIENT = asset_root("SWG_CLIENTDATA", "clientdata")
DEFAULT_TOOLS = asset_root("SWG_CLIENT_TOOLS", "client-tools")
GAME_MODEL_PATHS = ("data/sku.0/sys.shared/compiled/game", "data/sku.0/sys.client/built/game")

sys.path.insert(0, str(ROOT / "devtools/quests"))
from manifest_sources import Files, PLANETS, SERVER, SHARED, SCRIPTS, read_stf, source
from manifest_scripts import Script, arguments, literal, script_paths

sys.path.insert(0, str(ROOT / "devtools/docker"))
from validate_resource_distribution import table as read_iff_table


SERVER_OBJECTS = SERVER.parent / "object"
SHARED_OBJECTS = SHARED.parent / "object"
ACCEPTED_TEXT = {".tab", ".java", ".script", ".scriptlib", ".tpf"}
RESOURCE_TABLES = {SHARED / "resource/resource_tree.txt", SERVER / "resource/resource_distribution.txt"}
STRING_TABLE = re.compile(r"[a-zA-Z0-9_./-]+")
STRING_ID = re.compile(r"@?([a-zA-Z0-9_./\\-]+):([a-zA-Z0-9_./-]+)")


class GameFiles(Files):
    def __init__(self, revision):
        self.revision = revision
        self.blobs = {}
        resource_paths = {source(path)["path"] for path in RESOURCE_TABLES}
        roots = (SERVER, SHARED, SCRIPTS, SERVER_OBJECTS, SHARED_OBJECTS)
        command = ["git", "archive", "--format=tar", revision, "--", *(source(path)["path"] for path in roots)]
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE) as child:
            with tarfile.open(fileobj=child.stdout, mode="r|") as archive:
                for member in archive:
                    if member.isfile() and (PurePosixPath(member.name).suffix.lower() in ACCEPTED_TEXT
                                            or member.name in resource_paths):
                        with archive.extractfile(member) as stream:
                            self.blobs[member.name] = stream.read()
            if child.wait():
                raise RuntimeError(f"Cannot read game sources at {revision}")


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
            ("client-tools", client_tools / name) for name in (
                "string/en", "data/string/en", "data/sku.0/sys.shared/built/game/string/en",
                "data/sku.0/sys.client/built/game/string/en",
            )
        ]
        candidates.append(("clientdata", clientdata / "string/en"))
        self.roots = [(label, path) for label, path in candidates if path.is_dir()]
        self.cache = {}
        self.errors = {}

    def _read(self, table):
        relative = PurePosixPath(table + ".stf")
        problem = None
        for label, root in self.roots:
            path = root / relative
            try:
                return read_stf(path), f"{label}/string/en/{relative.as_posix()}", problem
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

    def conversation_names(self):
        names = set()
        for _, root in self.roots:
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
