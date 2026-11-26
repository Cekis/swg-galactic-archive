"""Source readers and shared identifiers for the quest manifest."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import re
import struct


ROOT = Path(".")
DATA = ROOT / "dsrc/sku.0"
SHARED = DATA / "sys.shared/compiled/game/datatables"
SERVER = DATA / "sys.server/compiled/game/datatables"
SCRIPTS = DATA / "sys.server/compiled/game/script"
PLANETS = {
    "corellia": "Corellia",
    "dantooine": "Dantooine",
    "dathomir": "Dathomir",
    "endor": "Endor",
    "hoth": "Hoth",
    "kashyyyk": "Kashyyyk",
    "lok": "Lok",
    "mustafar": "Mustafar",
    "naboo": "Naboo",
    "ord_mantell": "Ord Mantell",
    "rori": "Rori",
    "talus": "Talus",
    "tatooine": "Tatooine",
    "yavin4": "Yavin IV",
}
QUEST_ID = re.compile(r"[a-z0-9_/-]+", re.IGNORECASE)
TEST_ID = re.compile(r"(?:^|[/_])(?:_?debug|test|qa|example)(?:$|[/_\d])", re.IGNORECASE)


def source(path: Path, line: int | None = None) -> dict:
    result = {"path": path.relative_to(ROOT).as_posix()}
    if line is not None:
        result["line"] = line
    return result


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


@dataclass
class Row:
    path: Path
    line: int
    cells: dict[str, str]

    @property
    def source(self) -> dict:
        return source(self.path, self.line)

    def get(self, name: str, default: str = "") -> str:
        return self.cells.get(name, default)


def read_tab(path: Path, text: str) -> list[Row]:
    # DataTable sources use literal tabs and can omit trailing empty fields.
    # Quotes in field contents are preserved for callers that need the raw value.
    lines = text.split("\n")
    columns = lines[0].rstrip("\r").split("\t")
    return [
        Row(path, n, dict(zip(columns, (part.strip() for part in line.split("\t")))))
        for n, line in enumerate(lines[2:], 3)
        if line.strip()
    ]


class Files:
    """Read the selected source inputs by their logical Archive paths."""

    def __init__(self, blobs=None, revision=None, directory=None):
        self.revision = revision
        self.blobs = blobs
        self.directory = directory or ROOT

    def paths(self, directory, pattern, recursive=False):
        by_path = Path.as_posix
        if self.blobs is None:
            physical = self.directory / directory
            entries = physical.rglob(pattern) if recursive else physical.glob(pattern)
            return sorted((path.relative_to(self.directory) for path in entries), key=by_path)
        prefix = directory.relative_to(ROOT).as_posix() + "/"
        paths = []
        for path in self.blobs:
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):]
            if recursive:
                if fnmatchcase(relative.rsplit("/", 1)[-1], pattern):
                    paths.append(ROOT / path)
            elif relative.count("/") == pattern.count("/") and fnmatchcase(relative, pattern):
                paths.append(ROOT / path)
        return sorted(paths, key=by_path)

    def exists(self, path):
        return (self.directory / path).is_file() if self.blobs is None else path.relative_to(ROOT).as_posix() in self.blobs

    def text(self, path):
        data = (self.directory / path).read_bytes() if self.blobs is None else self.blobs[path.relative_to(ROOT).as_posix()]
        return decode_text(data)

    def tab(self, path):
        return read_tab(path, self.text(path)) if self.exists(path) else []


def values(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def number(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def present(value: str) -> bool:
    return bool(value and value.lower() not in {"none", "null"})


def quest_id(value: str, namespace: str = "quest") -> str:
    value = value.strip().replace("\\", "/")
    if value.startswith(("quest/", "spacequest/")) or value == "testquest":
        return value
    return f"{namespace}/{value}"


def planet(scene: str | None) -> str | None:
    if not scene:
        return None
    if scene.startswith("kashyyyk_"):
        return "kashyyyk"
    if scene == "yavin":
        return "yavin4"
    return scene if scene in PLANETS else None


def place(kind: str, evidence: dict, scene: str | None = None, **details) -> dict:
    return {"kind": kind, "planet": planet(scene), "scene": scene, "source": evidence, **details}


def add_unique(records: list, record: dict) -> None:
    if record not in records:
        records.append(record)


def edge(pending, origin, target, kind, evidence, **details):
    pending.append((origin, target, {"kind": kind, "source": evidence, **details}))


def reward(record, kind, timing, evidence, **details):
    add_unique(record["rewards"], {"kind": kind, "timing": timing, "source": evidence, **details})


def requirement(record, kind, evidence, **details):
    add_unique(record["requirements"], {"kind": kind, "source": evidence, **details})


def objective(record, scene, evidence):
    if scene and not any(site["scene"] == scene for site in record["objective_scenes"]):
        record["objective_scenes"].append({"scene": scene, "planet": planet(scene), "source": evidence})


def content_status(ident: str) -> str:
    return "development" if TEST_ID.search(ident) else "authored"


def new_record(ident: str, family: str, title: dict, sources: list[dict]) -> dict:
    return {
        "id": ident,
        "family": family,
        "source_status": content_status(ident),
        "title": title,
        "starts": [],
        "requirements": [],
        "predecessors": [],
        "follow_ups": [],
        "rewards": [],
        "objective_scenes": [],
        "issues": [],
        "sources": sources,
    }


def read_stf(path: Path) -> dict[str, str]:
    """Read the little-endian LocalizedStringTable 0000/0001 format."""
    data = path.read_bytes()
    magic, version, _, count = struct.unpack_from("<IBII", data)
    if magic != 0xABCD or version not in (0, 1) or count > len(data) // 20:
        raise ValueError(f"invalid STF header in {path.name}")
    offset = 13
    strings = {}
    for _ in range(count):
        ident, _, size = struct.unpack_from("<III", data, offset)
        start = offset + 12
        offset = start + size * 2
        if offset > len(data):
            raise ValueError(f"truncated STF text in {path.name}")
        strings[ident] = data[start:offset].decode("utf-16-le", errors="replace")
    resolved = {}
    for _ in range(count):
        ident, size = struct.unpack_from("<II", data, offset)
        start = offset + 8
        offset = start + size
        if offset > len(data) or ident not in strings:
            raise ValueError(f"invalid STF key in {path.name}")
        resolved[data[start:offset].decode("ascii")] = strings[ident]
    return resolved
