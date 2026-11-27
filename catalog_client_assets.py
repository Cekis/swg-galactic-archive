"""Read loose and packed SWG client assets through the same original paths."""

from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
from io import BytesIO, TextIOWrapper
from pathlib import Path, PurePosixPath
import re
import struct
from types import SimpleNamespace
import zlib


TOC_HEADER = struct.Struct("<4s4s4B6I")
TOC_ENTRY = struct.Struct("<BBHIIIII")
TRE_HEADER = struct.Struct("<4s4s7I")
TRE_ENTRY = struct.Struct("<I5i")
SEARCH_KEY = re.compile(r"search(path|tree|toc)(?:_(\d+)_(\d+)|(\d+))", re.I)
INCLUDE = re.compile(r'\.include\s+(?:"([^"]+)"|(\S+))', re.I)
STANDARD_TOC = re.compile(r"sku(\d+)_client\.toc", re.I)


def _local_path(root, value, base=None):
    path = ((base or root) / value.replace("\\", "/")).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Client asset path is outside {root}: {value}")
    return path


def _asset_key(value):
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part == ".." or ":" in part for part in path.parts):
        return None
    return path.as_posix().lower()


def _expanded(data, compression, length):
    if compression == 2:
        stream = zlib.decompressobj()
        data = stream.decompress(data, length + 1)
        if not stream.eof:
            raise ValueError("Incomplete or oversized zlib block.")
    elif compression != 0:
        raise ValueError(f"Unsupported TRE compression {compression}.")
    if len(data) != length:
        raise ValueError(f"Expected {length} bytes and read {len(data)}.")
    return data


@dataclass(frozen=True, slots=True)
class PackedAsset:
    archive: Path
    offset: int
    size: int
    compressed_size: int
    compression: int

    def read_bytes(self):
        try:
            with self.archive.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read(self.compressed_size if self.compression else self.size)
            return _expanded(data, self.compression, self.size)
        except (ValueError, zlib.error) as error:
            raise OSError(f"Could not read TRE entry in {self.archive} at byte {self.offset}. {error}") from error


def _toc_entries(root, path, archives):
    data = path.read_bytes()
    magic, version, toc_comp, name_comp, _, _, count, toc_len, name_len, expanded, trees, tree_len = (
        TOC_HEADER.unpack_from(data)
    )
    if (magic, version) != (b" COT", b"1000"):
        raise ValueError(f"Unsupported client TOC format in {path}.")
    cursor = TOC_HEADER.size
    tree_names = data[cursor:cursor + tree_len].decode("ascii").split("\0")
    if tree_names[-1] or len(tree_names) != trees + 1:
        raise ValueError(f"Invalid TRE list in {path}.")
    sources = []
    for name in tree_names[:-1]:
        source = _local_path(root, name)
        if not source.is_file():
            raise ValueError(f"Missing client archive {source} listed in {path.name}.")
        sources.append(source)
    archives.update(sources)
    cursor += tree_len
    table = _expanded(data[cursor:cursor + toc_len], toc_comp, count * TOC_ENTRY.size)
    cursor += toc_len
    names = _expanded(data[cursor:cursor + name_len], name_comp, expanded)
    if cursor + name_len != len(data):
        raise ValueError(f"Invalid size for client TOC {path}.")
    cursor = 0
    for compression, _, archive, _, length, offset, size, stored in TOC_ENTRY.iter_unpack(table):
        end = cursor + length
        if names[end:end + 1] != b"\0" or not 0 <= archive < len(sources):
            raise ValueError(f"Invalid file entry in {path}.")
        name = _asset_key(names[cursor:end].decode("ascii"))
        if name:
            yield name, PackedAsset(sources[archive], offset, size, stored, compression)
        cursor = end + 1
    if cursor != len(names):
        raise ValueError(f"Invalid filename block in {path}.")


def _tre_entries(path):
    with path.open("rb") as stream:
        magic, version, count, offset, toc_comp, toc_len, name_comp, name_len, expanded = (
            TRE_HEADER.unpack(stream.read(TRE_HEADER.size))
        )
        if (magic, version) == (b"EERT", b"6000"):
            raise ValueError(f"{path.name} needs its companion TOC. Configure searchTOC for that index.")
        if magic != b"EERT" or version not in (b"4000", b"5000"):
            raise ValueError(f"Unsupported standalone TRE format in {path}.")
        stream.seek(offset)
        table = _expanded(stream.read(toc_len if toc_comp else count * TRE_ENTRY.size),
                          toc_comp, count * TRE_ENTRY.size)
        names = _expanded(stream.read(name_len if name_comp else expanded), name_comp, expanded)
    for _, size, position, comp, stored, name_offset in TRE_ENTRY.iter_unpack(table):
        end = names.find(b"\0", name_offset)
        if not 0 <= name_offset <= end or min(size, position) < 0 or (comp and stored < 0):
            raise ValueError(f"Invalid file entry in {path}.")
        name = _asset_key(names[name_offset:end].decode("ascii"))
        if name:
            yield name, PackedAsset(path, position, size, stored, comp)


def _client_mounts(root):
    """Resolve SharedFile search settings and the included client configs."""
    settings = []
    visited = set()
    absolute_priority = None

    def parse(path, section=""):
        nonlocal absolute_priority
        if path in visited or not path.is_file():
            return
        visited.add(path)
        for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith(("#", ";", "//")) or not line:
                continue
            if match := INCLUDE.match(line):
                parse(_local_path(root, match[1] or match[2], path.parent), section)
            elif line.startswith("[") and "]" in line:
                section = line[1:line.index("]")].lower()
            elif section == "sharedfile":
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                key = key.strip()
                value = re.split(r"\s+[;#]", value.strip(), maxsplit=1)[0].strip().strip('"')
                if key.lower() == "searchabsolute" and value.lstrip("-").isdigit():
                    absolute_priority = int(value)
                elif value and (match := SEARCH_KEY.fullmatch(key)):
                    kind, sku, priority, legacy = match.groups()
                    settings.append((int(priority or legacy), int(sku or 0), kind.lower(), _local_path(root, value)))
    config = root / "client.cfg"
    parse(config)
    if not any(kind in ("toc", "tree") for _, _, kind, _ in settings) and not config.is_file():
        for path in root.glob("*.toc"):
            if match := STANDARD_TOC.fullmatch(path.name):
                sku = int(match[1])
                settings.append((sku, sku, "toc", path))

    if not settings:
        if config.is_file() and any(root.glob("*.tre")):
            raise ValueError(f"No client archives are configured in {config}. Check its SharedFile search entries.")
        return (), visited

    order = {"path": 0, "tree": 1, "toc": 2}
    ordered = sorted(settings, key=lambda entry: (-entry[0], entry[1], order[entry[2]]))
    priority = max((entry[0] for entry in ordered), default=0) + 1 if absolute_priority is None else absolute_priority
    # The client's root is mounted after the configured sources at equal priority.
    index = next((i for i, entry in enumerate(ordered) if entry[0] < priority), len(ordered))
    ordered.insert(index, (priority, 0, "path", root))
    return ordered, visited


class ClientInstallation:
    def __init__(self, root, mounts, configs):
        self.root = root
        self.archive_files = {}
        self.directories = defaultdict(set)
        self.loose = []
        archives = set()
        for rank, (_, _, kind, path) in enumerate(mounts):
            if kind == "path":
                if path.is_dir():
                    self.loose.append((rank, path, {entry.name.lower() for entry in path.iterdir()}))
                continue
            if not path.is_file():
                raise ValueError(f"Missing configured client {kind.upper()} {path}.")
            archives.add(path)
            try:
                entries = _toc_entries(root, path, archives) if kind == "toc" else _tre_entries(path)
                for name, entry in entries:
                    self.archive_files.setdefault(name, (rank, entry))
            except (struct.error, UnicodeError, zlib.error) as error:
                raise ValueError(f"Could not read the client archive index {path}. {error}") from error
        for name in self.archive_files:
            while name:
                parent, _, child = name.rpartition("/")
                if child in self.directories[parent]:
                    break
                self.directories[parent].add(child)
                name = parent
        stamps = []
        for path in sorted(archives | configs):
            info = path.stat()
            stamps.append((path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns))
        self.signature = sha256(repr((mounts, stamps)).encode("utf-8")).hexdigest()
        self.archive_count = sum(path.suffix.lower() == ".tre" for path in archives)
        self.index_count = sum(path.suffix.lower() == ".toc" for path in archives)

    def loose_paths(self, relative, last_rank=None):
        top_level = relative.parts[0].lower() if relative.parts else None
        for rank, root, entries in self.loose:
            if last_rank is not None and rank > last_rank:
                break
            if top_level is not None and top_level not in entries:
                continue
            path = (root / relative.as_posix()).resolve()
            if path.is_relative_to(root):
                yield path

    def file(self, relative):
        packed = self.archive_files.get(relative.as_posix().lower())
        for path in self.loose_paths(relative, packed[0] if packed else None):
            if path.is_file():
                return path
        return packed[1] if packed else None


@dataclass(frozen=True, slots=True, eq=False)
class ClientAssetPath:
    """Read-only logical path in a configured client installation."""

    installation: ClientInstallation
    relative: PurePosixPath = PurePosixPath()

    def __str__(self):
        return str(self.installation.root / self.relative.as_posix())

    def __lt__(self, other):
        return str(self) < str(other)

    def __eq__(self, other):
        return isinstance(other, ClientAssetPath) and (
            self.installation.root, self.relative
        ) == (other.installation.root, other.relative)

    def __hash__(self):
        return hash((self.installation.root, self.relative))

    def __truediv__(self, value):
        return ClientAssetPath(self.installation, self.relative / str(value).replace("\\", "/"))

    @property
    def name(self):
        return self.relative.name or self.installation.root.name

    @property
    def suffix(self):
        return self.relative.suffix

    @property
    def stem(self):
        return self.relative.stem

    def resolve(self):
        return self

    def relative_to(self, base):
        if not self._safe:
            raise ValueError(f"Invalid client asset path {self.relative}.")
        return (self.installation.root / self.relative.as_posix()).relative_to(str(base))

    def is_relative_to(self, base):
        return self._safe and (self.installation.root / self.relative.as_posix()).is_relative_to(str(base))

    @property
    def _safe(self):
        return self.relative == PurePosixPath() or bool(_asset_key(self.relative.as_posix()))

    def _file(self):
        return self.installation.file(self.relative) if self._safe else None

    def is_file(self):
        return self._file() is not None

    def is_dir(self):
        if not self._safe:
            return False
        key = "" if self.relative == PurePosixPath() else self.relative.as_posix().lower()
        return key in self.installation.directories or any(
            path.is_dir() for path in self.installation.loose_paths(self.relative))

    def iterdir(self):
        if not self._safe:
            return
        key = "" if self.relative == PurePosixPath() else self.relative.as_posix().lower()
        children = {name: name for name in self.installation.directories.get(key, ())}
        for path in reversed(tuple(self.installation.loose_paths(self.relative))):
            if path.is_dir():
                children.update((entry.name.lower(), entry.name) for entry in path.iterdir())
        for name in children.values():
            yield self / name

    def glob(self, pattern):
        return (path for path in self.iterdir() if fnmatchcase(path.name.lower(), pattern.lower()))

    def rglob(self, pattern):
        if not self._safe:
            return
        prefix = "" if self.relative == PurePosixPath() else self.relative.as_posix().lower() + "/"
        names = {key: key[len(prefix):] for key in self.installation.archive_files
                 if key.startswith(prefix) and fnmatchcase(key.rpartition("/")[2], pattern.lower())}
        for path in reversed(tuple(self.installation.loose_paths(self.relative))):
            if path.is_dir():
                for entry in path.rglob("*"):
                    if fnmatchcase(entry.name.lower(), pattern.lower()):
                        relative = entry.relative_to(path).as_posix()
                        names[prefix + relative.lower()] = relative
        for value in names.values():
            yield self / value

    def stat(self):
        asset = self._file()
        if isinstance(asset, Path):
            return asset.stat()
        if asset:
            return SimpleNamespace(st_size=asset.size, st_mtime_ns=asset.archive.stat().st_mtime_ns)
        raise FileNotFoundError(str(self))

    def read_bytes(self):
        asset = self._file()
        if asset:
            return asset.read_bytes()
        raise FileNotFoundError(str(self))

    def open(self, mode="r", encoding=None, errors=None):
        if mode not in ("r", "rt", "rb"):
            raise ValueError("Client assets are read only.")
        asset = self._file()
        if isinstance(asset, Path):
            return asset.open(mode, encoding=encoding, errors=errors)
        if not asset:
            raise FileNotFoundError(str(self))
        stream = BytesIO(asset.read_bytes())
        return stream if mode == "rb" else TextIOWrapper(stream, encoding=encoding, errors=errors)

    def read_text(self, encoding=None, errors=None):
        with self.open("r", encoding=encoding, errors=errors) as stream:
            return stream.read()


def resolve_asset_path(path):
    return path if isinstance(path, ClientAssetPath) else Path(path).resolve()


def client_asset_root(directory):
    root = Path(directory).resolve()
    mounts, configs = _client_mounts(root)
    return ClientAssetPath(ClientInstallation(root, mounts, configs)) if mounts else root
