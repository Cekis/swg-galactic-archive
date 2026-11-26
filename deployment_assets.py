"""Verify the packaged assets and install any pinned GitHub references."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from stat import S_ISREG
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from urllib.request import urlopen
from zipfile import ZipFile


MANIFEST = "asset-manifest.json"
ASSET_ROOTS = (".local/model-assets/", "clientdata/", "client-tools/", "game/data/")
GITHUB_REPOS = {"mesh", "serverdata", "client-assets"}
GIT_SHA = re.compile(r"[0-9a-f]{40}")
FILE_SHA = re.compile(r"[0-9a-f]{64}")
FILE_NAME = re.compile(r"[a-zA-Z0-9_./ -]+")
BLOCK = 256 * 1024


def relative_path(value):
    if (not isinstance(value, str) or not FILE_NAME.fullmatch(value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError(f"Invalid package path {value!r}")
    return PurePosixPath(value)


def asset_path(root, entry):
    name = entry["path"]
    if not name.startswith(ASSET_ROOTS):
        raise ValueError(f"Unexpected asset path {name!r}")
    path = (root / relative_path(name)).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Asset escapes the package: {name}")
    return path


def asset_manifest(root):
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("mode") not in ("bundled", "github"):
        raise ValueError("Unknown Galactic Archive asset manifest.")
    for entry in manifest["assets"]:
        relative_path(entry["path"])
        if type(entry["bytes"]) is not int or entry["bytes"] < 0 or not FILE_SHA.fullmatch(entry["sha256"]):
            raise ValueError(f"Invalid asset metadata for {entry['path']}")
        if not entry["bundled"]:
            github_source(entry["github"])
    return manifest


def github_source(source):
    repo = source["repository"].removeprefix("SWG-Source/")
    revision = source["revision"]
    if repo not in GITHUB_REPOS or source["repository"] != f"SWG-Source/{repo}" or not GIT_SHA.fullmatch(revision):
        raise ValueError("Invalid pinned SWG Source repository reference.")
    return repo, revision, relative_path(source["path"]).as_posix()


def file_hash(path, buffer=None):
    digest = sha256()
    chunk = memoryview(buffer if buffer is not None else bytearray(BLOCK))
    with path.open("rb") as stream:
        while size := stream.readinto(chunk):
            digest.update(chunk[:size])
    return digest.hexdigest()


def size_matches(path, size):
    try:
        info = path.stat()
        return S_ISREG(info.st_mode) and info.st_size == size
    except OSError:
        return False


def missing_reference_assets(root):
    manifest = asset_manifest(root)
    for entry in manifest["assets"]:
        if not entry["bundled"] and not size_matches(asset_path(root, entry), entry["bytes"]):
            return True
    return False


def install_file(stream, target, entry):
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256()
    size = 0
    with NamedTemporaryFile(dir=target.parent, prefix="asset-", suffix=".pending", delete=False) as out:
        pending = Path(out.name)
        try:
            while chunk := stream.read(min(BLOCK, entry["bytes"] - size + 1)):
                size += len(chunk)
                if size > entry["bytes"]:
                    raise ValueError(f"Unexpected GitHub asset size for {entry['path']}")
                digest.update(chunk)
                out.write(chunk)
            if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError(f"GitHub asset checksum mismatch for {entry['path']}")
            out.close()
            pending.replace(target)
        finally:
            out.close()
            pending.unlink(missing_ok=True)


def download_file(target, entry):
    repo, revision, relative = github_source(entry["github"])
    url = f"https://raw.githubusercontent.com/SWG-Source/{repo}/{revision}/{quote(relative)}"
    with urlopen(url, timeout=30) as source:
        install_file(source, target, entry)


def verify_or_install(root, cache=None, *, download=False, verify_bundled=True):
    root = root.resolve()
    manifest = asset_manifest(root)
    installed = 0
    remote = []
    buffer = bytearray(BLOCK)
    with ExitStack() as stack:
        snapshots = {}
        for entry in manifest["assets"]:
            target = asset_path(root, entry)
            if (size_matches(target, entry["bytes"]) and
                    ((entry["bundled"] and not verify_bundled) or file_hash(target, buffer) == entry["sha256"])):
                continue
            if entry["bundled"] or (cache is None and not download):
                raise ValueError(f"Missing or damaged asset {entry['path']}")
            repo, revision, relative = github_source(entry["github"])
            key = repo, revision
            if key not in snapshots:
                name = f"{repo}-{revision}.zip"
                archive = cache / name if cache else None
                if archive and archive.is_file():
                    snapshots[key] = stack.enter_context(ZipFile(archive))
                elif download:
                    snapshots[key] = None
                else:
                    raise ValueError(f"Supply {name} in {cache}, or use --download.")
            if snapshots[key] is None:
                remote.append((target, entry))
                continue
            with snapshots[key].open(f"{repo}-{revision}/{relative}") as stream:
                install_file(stream, target, entry)
            installed += 1
    if remote:
        print(f"Fetching {len(remote):,} selected files from pinned GitHub revisions", flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(download_file, target, entry) for target, entry in remote]
            try:
                for task in as_completed(jobs):
                    task.result()
                    installed += 1
            finally:
                for task in jobs:
                    task.cancel()
    return len(manifest["assets"]), installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, help="Reuse pinned GitHub ZIPs from this directory.")
    parser.add_argument("--download", action="store_true", help="Fetch missing asset files from their pinned revisions.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    installing = args.snapshot_dir is not None or args.download
    total, installed = verify_or_install(root, args.snapshot_dir, download=args.download, verify_bundled=not installing)
    print(f"{total:,} assets checked, {installed:,} installed from pinned GitHub revisions.")


if __name__ == "__main__":
    main()
